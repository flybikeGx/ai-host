from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from .config import Config, validate_selection
from .engines import codeagent_command, dsh_command, dsh_patch, parse_dsh_output, runtime_environment
from .package import build_prompt, expand, freeze, instruction_hash, within, load_package
from .processes import Cancelled, DeadlineExceeded, alive, identity, kill_tree, run_process, spawn_options
from .store import ACTIVE, TERMINAL, Store


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.data)

    def submit(self, repo: str, task: str | None = None, *, ref: str | None = None,
               model: str | None = None, effort: str | None = None, install_only: bool = False) -> dict:
        spec = self.specification(repo, task, ref=ref, model=model, effort=effort, install_only=install_only)
        queued = [r for r in self.store.list(100) if r["status"] == "queued"]
        if len(queued) >= 30:
            raise ValueError("待执行任务已达到 30 个，请稍后提交")
        return self._launch(self.store.create(spec))

    def specification(self, repo: str, task: str | None = None, *, ref: str | None = None,
                      model: str | None = None, effort: str | None = None, install_only=False) -> dict:
        if not isinstance(repo, str) or not repo.strip() or repo.startswith("-"):
            raise ValueError("repo 必须是 Git 仓库 URL 或本地 Git 仓库路径")
        if repo.startswith(("http://", "https://")) and urlparse(repo).username:
            raise ValueError("不要在 repo URL 中放凭据，请使用 Git credential helper")
        if ref and (ref.startswith("-") or any(x in ref for x in ("\0", "\n"))):
            raise ValueError("无效 Git ref")
        local = Path(repo).expanduser()
        if local.exists():
            repo = str(local.resolve())
        if task is not None and (not isinstance(task, str) or len(task.encode()) > 256 * 1024):
            raise ValueError("task 必须是不超过 256 KiB 的文本")
        runtime = {**self.config.runtime}
        if model:
            runtime["model"] = model
        if effort:
            runtime["effort"] = effort
        validate_selection(runtime)
        if not runtime.get("model"):
            raise ValueError("请配置 model")
        if runtime.get("engine", "dsh") == "codeagent" and effort and effort not in {"low", "medium", "high", "max"}:
            raise ValueError("CodeAgent effort 只支持 low/medium/high/max")
        if runtime.get("engine") == "codeagent" and self.config.max_concurrent != 1:
            raise ValueError("CodeAgent 首版要求 queue.max_concurrent=1，避免共享登录配置并发冲突")
        spec = {"repo": repo, "ref": ref, "task": task, "runtime": runtime,
                "install_only": install_only, "max_concurrent": self.config.max_concurrent}
        from .repositories import Repositories
        installed = Repositories(self.config).latest(repo, ref)
        if installed:
            spec.update(installation_id=installed["id"], temporary_workspace=True)
        return spec

    def _launch(self, job: dict) -> dict:
        env = runtime_environment(job["spec"]["runtime"], Path(job["root"]) / "dsh-home")
        with (Path(job["root"]) / "supervisor.log").open("ab") as log:
            try:
                proc = subprocess.Popen([sys.executable, "-m", "agent_base", "--config", str(self.config.path), "_execute", job["id"]],
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **spawn_options())
                self.store.update(job["id"], pid=proc.pid, birth=identity(proc.pid))
            except OSError as error:
                self.store.update(job["id"], status="failed", error=f"启动任务进程失败：{error}")
        return self.store.get(job["id"])

    def start_prepared(self, run_id: str, task: str | None = None) -> dict:
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT spec,status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] != "prepared":
                raise ValueError("只有 prepared 任务可以 start；运行中或已执行任务不会重复提交")
            spec = json.loads(row["spec"])
            spec.update(install_only=False, task=task or spec.get("task"))
            conn.execute("UPDATE runs SET spec=?,status='queued',pid=NULL,birth=NULL,executor_started=0,result=NULL,error=NULL,finished_at=NULL,session_status=NULL,updated=? WHERE id=?",
                         (json.dumps(spec), time.time(), run_id))
        return self._launch(self.store.get(run_id))

    def reconcile(self):
        # The original process cannot survive a host restart. Do not replay it.
        with self.store.connection() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM runs WHERE dispatched=1 AND status IN ('queued','preparing','running','verifying')")]
        for run_id in ids:
            job = self.store.get(run_id)
            if not job["dispatched"]:
                continue  # Durable queue entries have no process until dispatch.
            if job["status"] not in ACTIVE | {"queued"} or time.time() - job["updated"] < 3:
                continue
            if not alive(job["pid"], job["birth"]):
                # An executor can finish between the snapshot and liveness
                # check. Never overwrite a newer terminal state.
                with self.store.connection() as conn:
                    changed = conn.execute("UPDATE runs SET status='interrupted',session_status='interrupted',finished_at=?,error=?,updated=? WHERE id=? AND status=? AND updated=?",
                        (time.time(), "任务监督进程已退出；未自动重试。请核对原始日志和外部操作。", time.time(), job["id"], job["status"], job["updated"])).rowcount
                if changed:
                    if alive(job["process_pid"], job["process_birth"]):
                        kill_tree(job["process_pid"], job["process_birth"])
                    self.store.event(job["id"], "run.interrupted", {})

    def get(self, run_id: str) -> dict:
        self.reconcile()
        return self.store.get(run_id)

    def list(self, limit=30) -> list[dict]:
        self.reconcile()
        return self.store.list(limit)

    def cancel(self, run_id: str) -> dict:
        job = self.get(run_id)
        if job["status"] not in TERMINAL:
            with self.store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE runs SET cancel_requested=1,updated=? WHERE id=?", (time.time(), run_id))
                conn.execute("UPDATE runs SET status='cancelled',finished_at=?,session_status='not_started' WHERE id=? AND status='queued' AND dispatched=0", (time.time(), run_id))
            self.store.event(run_id, "run.cancel_requested", {})
        return self.store.get(run_id)

    def wait(self, run_id: str, seconds: float = 30) -> dict:
        until = time.time() + max(0, min(seconds, 300))
        while True:
            job = self.get(run_id)
            if job["status"] in TERMINAL or time.time() >= until:
                return job
            time.sleep(0.2)

    def execute(self, run_id: str):
        # Atomically elect one owner even while the task is still queued.
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE runs SET executor_started=1,pid=?,birth=?,updated=? WHERE id=? AND status='queued' AND executor_started=0",
                                   (os.getpid(), identity(os.getpid()), time.time(), run_id)).rowcount
        if not changed:
            return
        job = self.store.get(run_id)
        cancel = lambda: bool(self.store.get(run_id)["cancel_requested"])
        signal.signal(signal.SIGTERM, lambda *_: self.store.update(run_id, cancel_requested=1))
        signal.signal(signal.SIGINT, lambda *_: self.store.update(run_id, cancel_requested=1))
        root = Path(job["root"])
        spec = job["spec"]
        runtime = spec["runtime"]
        env = runtime_environment(runtime, root / "dsh-home")
        temporary = None
        try:
            while not self.store.claim(run_id, spec["max_concurrent"]):
                if cancel():
                    raise Cancelled("排队任务已取消")
                if self.store.get(run_id)["status"] != "queued":
                    return
                self.reconcile()
                self.store.update(run_id, pid=os.getpid(), birth=identity(os.getpid()))
                time.sleep(0.2)
            self.store.event(run_id, "run.preparing", {})
            started = time.time()
            deadline = started + runtime.get("timeout_seconds", 1800)
            self.store.update(run_id, started_at=started, deadline=deadline, heartbeat=started, session_status="not_started")
            if spec.get("temporary_workspace"):
                temp_root = self.config.data / "workspaces"
                temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = Path(tempfile.mkdtemp(prefix=run_id + "-", dir=temp_root))
            if job["prepared"] and job.get("workspace"):
                if temporary:
                    temporary.rmdir()
                workspace = Path(job["workspace"])
                temporary = workspace.parent if spec.get("temporary_workspace") else None
            else:
                workspace = (temporary or root) / "repo"
            self.store.update(run_id, workspace=str(workspace))

            last_heartbeat = 0
            def heartbeat():
                nonlocal last_heartbeat
                if time.time() - last_heartbeat >= 1:
                    self.store.update(run_id, heartbeat=time.time())
                    last_heartbeat = time.time()

            def command(args, label, *, cwd=workspace, timeout=None, command_env=None):
                until = min(deadline, time.time() + timeout) if timeout else deadline
                self.store.event(run_id, "process.started", {"label": label})
                code = run_process(args, cwd=cwd, env=command_env or env, output=root / f"{label}.stdout.log", error=root / f"{label}.stderr.log",
                                   deadline=until, cancelled=cancel, on_started=lambda pid, birth: self.store.update(run_id, process_pid=pid, process_birth=birth), on_poll=heartbeat)
                self.store.update(run_id, process_pid=None, process_birth=None)
                self.store.event(run_id, "process.finished", {"label": label, "exit_code": code})
                if code:
                    raise RuntimeError(f"{label} 失败，退出码 {code}；见 {root / (label + '.stderr.log')}")

            if not job["prepared"]:
                from .repositories import Repositories
                repositories = Repositories(self.config)
                installation_id = spec.get("installation_id")
                if not installation_id:
                    git_env = {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
                    command(["git", "-c", "core.hooksPath=" + os.devnull, "clone", "--no-local", "--no-checkout", "--", spec["repo"], str(workspace)],
                            "clone", cwd=root, command_env=git_env)
                    ref = spec.get("ref") or "HEAD"
                    command(["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], "resolve-ref")
                    sha = (root / "resolve-ref.stdout.log").read_text().strip()
                    command(["git", "-c", "core.hooksPath=" + os.devnull, "checkout", "--detach", sha], "checkout", command_env=git_env)
                    preliminary = load_package(workspace)
                    if preliminary["schema_version"] == 2:
                        installed = repositories.install(spec["repo"], spec.get("ref"), checkout=workspace, commit=sha,
                                                           deadline=deadline, cancelled=cancel)
                        installation_id = installed["id"]
                        spec["installation_id"] = installation_id
                        self.store.update(run_id, spec=spec)
                if installation_id:
                    installed = repositories.bind(installation_id, job, workspace)
                    manifest = installed["manifest"]
                    self.store.event(run_id, "repo.bound", {"installation_id": installation_id, "namespace": installed["namespace"]})
                if spec.get("workspace_source"):
                    from .scheduler import copy_inputs
                    copy_inputs(Path(spec["workspace_source"]), workspace / "input", deadline, cancel)
                if spec.get("activation"):
                    activation_file = workspace / "activation.json"
                    if activation_file.exists() or activation_file.is_symlink():
                        raise ValueError("仓库不可预置保留文件 activation.json")
                    activation_file.write_text(json.dumps(spec["activation"], ensure_ascii=False, indent=2), encoding="utf-8")
                if not installation_id:
                    manifest = freeze(workspace, root / "package")
                    receipt = {"repo": spec["repo"], "commit": sha, "instructions_sha256": instruction_hash(root / "package"), "runtime": runtime}
                    (root / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                    if manifest.get("environment", {}).get("lockfile"):
                        if not shutil.which("uv"):
                            raise ValueError("安装需要预先配置 uv")
                        install_env = {**env, "UV_PROJECT_ENVIRONMENT": str(root / "env"), "UV_CACHE_DIR": str(self.config.data / "uv-cache")}
                        command(["uv", "sync", "--frozen", "--no-dev", "--project", str(root / "package")], "dependencies", cwd=root, command_env=install_env)
                    python = str(root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")) if (root / "env").exists() else sys.executable
                    for i, step in enumerate(manifest.get("installation", {}).get("steps", [])):
                        command(expand(step["argv"], root / "package", workspace, python), f"install-{i}", timeout=step.get("timeout_seconds", 120))
                    (root / "interpreter.txt").write_text(python, encoding="utf-8")
                self.store.update(run_id, prepared=1)
            manifest = json.loads((root / "package" / "resolved.json").read_text(encoding="utf-8"))
            if manifest.get("limits", {}).get("wall_timeout_seconds"):
                deadline = min(deadline, started + float(manifest["limits"]["wall_timeout_seconds"]))
                self.store.update(run_id, deadline=deadline)
            python = (root / "interpreter.txt").read_text(encoding="utf-8")
            if spec.get("installation_id"):
                from .repositories import Repositories
                repositories = Repositories(self.config)
                repositories.ensure_service(spec["installation_id"])
            if spec["install_only"]:
                self.store.update(run_id, status="prepared", result={"workspace": str(workspace), "receipt": str(root / "receipt.json")})
                self.store.event(run_id, "run.prepared", {})
                return
            task = spec.get("task") or manifest.get("task", {}).get("default")
            if not task or not task.strip():
                raise ValueError("没有本次任务。请提交 task，或在 agent.yaml 中声明 task.default。可先 install 检查仓库。")
            prompt = root / "prompt.txt"
            if spec.get("installation_id"):
                from .repo_runtime import repo_prompt
                prompt.write_text(repo_prompt(root / "package", workspace, manifest, task), encoding="utf-8")
            else:
                prompt.write_text(build_prompt(root / "package", workspace, manifest, task), encoding="utf-8")
            engine = runtime.get("engine", "dsh")
            if engine == "dsh":
                if spec.get("installation_id"):
                    from .repo_runtime import repo_patch
                    patch = repo_patch(runtime, root, repositories, spec["installation_id"], run_id, env)
                else:
                    patch = dsh_patch(runtime, root, package=root / "package", workspace=workspace, manifest=manifest, python=python)
                args = dsh_command(runtime, patch)
            else:
                if manifest.get("tools", {}).get("local") or manifest.get("mcp", {}).get("servers"):
                    raise ValueError("CodeAgent adapter 尚未验证 MCP 挂载，不会静默忽略清单工具；请在内网适配 engines.py")
                args = codeagent_command(runtime)
            self.store.update(run_id, status="running", session_status="starting")
            self.store.event(run_id, "run.started", {"engine": engine})
            output = root / "runtime.jsonl"
            def runtime_started(pid, birth):
                self.store.update(run_id, process_pid=pid, process_birth=birth)
                self.store.event(run_id, "runtime.process_started", {"pid": pid, "birth": birth})
            cursor = 0
            def runtime_poll():
                nonlocal cursor
                heartbeat()
                # Capture session opening before the CLI finishes (including timeout).
                if engine != "dsh" or self.store.get(run_id)["session_id"]:
                    return
                with output.open("rb") as stream:
                    stream.seek(cursor)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break
                        cursor += len(line)
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and event.get("type") == "session" and isinstance(event.get("sessionId"), str):
                            self.store.update(run_id, session_id=event["sessionId"], session_status="running")
                            self.store.event(run_id, "session.opened", {"session_id": event["sessionId"]})
                            break
            code = run_process(args, cwd=workspace, env=env, output=output, error=root / "runtime.stderr.log", input_file=prompt,
                               deadline=deadline, cancelled=cancel, on_started=runtime_started, on_poll=runtime_poll)
            runtime_poll()
            self.store.update(run_id, process_pid=None, process_birth=None)
            if code:
                raise RuntimeError(f"{engine} 退出码 {code}；见 {root / 'runtime.stderr.log'} 和 runtime.jsonl")
            result = parse_dsh_output(output) if engine == "dsh" else {"text": output.read_text(encoding="utf-8"), "session_id": None}
            self.store.update(run_id, session_id=result.get("session_id"), status="verifying", session_status="finished")
            for i, check in enumerate(manifest.get("completion", {}).get("checks", [])):
                command(expand(check["argv"], root / "package", workspace, python), f"verify-{i}", timeout=check.get("timeout_seconds", 120))
            artifacts = []
            for artifact in manifest.get("completion", {}).get("artifacts", []):
                file = within(workspace, artifact["path"], exists=False)
                if not file.is_file():
                    if artifact.get("required", True):
                        raise ValueError(f"产物不存在：{artifact['path']}")
                    continue
                if file.stat().st_size < artifact.get("min_bytes", 0):
                    raise ValueError(f"产物内容不足：{artifact['path']}")
                if file.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("单产物超过首版 64 MiB 上限")
                target = root / "artifacts" / artifact["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
                artifacts.append({"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "size": target.stat().st_size})
            if cancel():
                raise Cancelled("任务已取消")
            if time.time() >= deadline:
                raise DeadlineExceeded("产物验证超时")
            retained = not bool(temporary)
            if temporary:
                try:
                    shutil.rmtree(temporary)
                    self.store.event(run_id, "workspace.cleaned", {"path": str(temporary)})
                except OSError as error:
                    retained = True
                    self.store.event(run_id, "workspace.cleanup_failed", {"error": str(error)})
            result.update(artifacts=artifacts, workspace=str(workspace),
                          workspace_retained=retained,
                          verification="checks_passed" if manifest.get("completion", {}).get("checks") else "structural_only")
            (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.store.update(run_id, status="succeeded", result=result)
            self.store.event(run_id, "run.finished", {"status": "succeeded"})
        except BaseException as error:
            traceback.print_exc()
            status = "cancelled" if isinstance(error, Cancelled) else "timed_out" if isinstance(error, DeadlineExceeded) else "failed"
            self.store.update(run_id, status=status, session_status=status, error=str(error))
            self.store.event(run_id, "run.finished", {"status": status, "error": str(error)})
        finally:
            self.store.update(run_id, process_pid=None, process_birth=None)
