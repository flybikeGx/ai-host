"""Persistent repo installations. Runtime processes never own these MCP services."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .engines import runtime_environment
from .package import expand, freeze, load_package, within, instruction_hashes
from .processes import alive, identity, kill_tree, run_process, spawn_options
from .store import Store


@contextmanager
def installation_lock(path: Path, timeout=180):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        until = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    stream.write(b"0")
                    stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise ValueError("等待仓库安装锁超时")
                time.sleep(0.1)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Repositories:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.data)

    def get(self, installation_id):
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM repo_installations WHERE id=?", (installation_id,)).fetchone()
        if not row:
            raise ValueError(f"安装不存在：{installation_id}")
        item = dict(row)
        item["manifest"] = json.loads(item["manifest"])
        item["namespace"] = "repo_" + item["repo_id"]
        item["service_alive"] = alive(item["pid"], item["birth"])
        return item

    def list(self):
        with self.store.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM repo_installations ORDER BY created DESC")]
        return [self.get(i) for i in ids]

    def latest(self, origin, ref=None):
        origin = str(Path(origin).expanduser().resolve()) if Path(origin).expanduser().exists() else origin
        for item in self.list():
            if item["origin"] == origin and item["status"] == "ready" and item["enabled"]:
                receipt = json.loads((Path(item["root"]) / "receipt.json").read_text())
                if receipt.get("ref") == ref or ref == item["commit_sha"]:
                    return item
        return None

    def update(self, installation_id, **fields):
        allowed = {"status", "pid", "birth", "endpoint", "error", "enabled", "restart_attempts", "next_retry"}
        if not fields or fields.keys() - allowed:
            raise ValueError("无效安装字段")
        with self.store.connection() as conn:
            conn.execute(f"UPDATE repo_installations SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), installation_id))

    def install(self, origin, ref=None, *, checkout=None, commit=None, deadline=None, cancelled=lambda: False):
        origin = str(Path(origin).expanduser().resolve()) if Path(origin).expanduser().exists() else origin
        if not origin or origin.startswith("-"):
            raise ValueError("无效仓库")
        if origin.startswith(("http://", "https://")) and urlparse(origin).username:
            raise ValueError("仓库 URL 不得包含凭据，请使用 Git credential helper")
        if ref and (ref.startswith("-") or any(c in ref for c in "\0\n")):
            raise ValueError("无效 ref")
        repo_id = hashlib.sha256(origin.encode()).hexdigest()[:16]
        repo_root = self.config.data / "repos" / repo_id
        deadline = deadline or time.time() + 600
        with installation_lock(repo_root / "install.lock", max(0.1, deadline - time.time())):
            if checkout is None:
                clone = repo_root / ("checkout-" + secrets.token_hex(6))
                env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
                try:
                    code = run_process(["git", "-c", "core.hooksPath=" + os.devnull, "clone", "--no-local", "--no-checkout", "--", origin, str(clone)],
                        cwd=repo_root, env=env, output=repo_root / "clone.log", error=repo_root / "clone.stderr.log", deadline=deadline, cancelled=cancelled)
                    if code:
                        raise ValueError("clone 失败，见仓库 clone.stderr.log")
                    result = subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--end-of-options", (ref or "HEAD") + "^{commit}"],
                                            capture_output=True, text=True, timeout=max(0.1, deadline-time.time()))
                    if result.returncode:
                        raise ValueError("无法解析仓库 commit")
                    commit = result.stdout.strip()
                    subprocess.run(["git", "-C", str(clone), "-c", "core.hooksPath=" + os.devnull, "checkout", "--detach", commit],
                                   check=True, capture_output=True, timeout=max(0.1, deadline-time.time()))
                    item = self._install_checkout(origin, ref, repo_id, clone, commit, deadline, cancelled)
                finally:
                    shutil.rmtree(clone, ignore_errors=True)
            else:
                item = self._install_checkout(origin, ref, repo_id, checkout, commit, deadline, cancelled)
        return item

    def _install_checkout(self, origin, ref, repo_id, checkout, commit, deadline, cancelled):
        manifest = load_package(checkout)
        if manifest["schema_version"] != 2:
            raise ValueError("repo 长期安装要求 schema_version: 2；旧清单仍可使用原有 run/install")
        digest = hashlib.sha256((repo_id + commit + json.dumps(manifest, sort_keys=True)).encode()).hexdigest()[:24]
        install_root = self.config.data / "repos" / repo_id / "installations" / digest
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM repo_installations WHERE id=?", (digest,)).fetchone()
        if row and row["status"] == "ready":
            self.update(digest, enabled=1)
            self.ensure_service(digest)
            return self.get(digest)
        if row and alive(row["pid"], row["birth"]):
            kill_tree(row["pid"], row["birth"])
        if install_root.exists():
            shutil.rmtree(install_root)
        install_root.mkdir(parents=True, mode=0o700)
        source = install_root / "source"
        manifest = freeze(checkout, source)
        with self.store.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO repo_installations(id,repo_id,origin,commit_sha,root,manifest,status,created) VALUES(?,?,?,?,?,?,'installing',?)",
                (digest, repo_id, origin, commit, str(install_root), json.dumps(manifest), time.time()))
        try:
            python = sys.executable
            if manifest.get("environment", {}).get("lockfile"):
                env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(install_root / "env"), "UV_CACHE_DIR": str(self.config.data / "uv-cache")}
                code = run_process(["uv", "sync", "--frozen", "--no-dev", "--project", str(source)], cwd=source, env=env,
                                   output=install_root / "dependencies.log", error=install_root / "dependencies.stderr.log", deadline=deadline, cancelled=cancelled)
                if code:
                    raise ValueError("安装依赖失败")
                python = str(install_root / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            for index, step in enumerate(manifest.get("installation", {}).get("steps", [])):
                code = run_process(expand(step["argv"], source, source, python), cwd=source, env=dict(os.environ),
                    output=install_root / f"install-{index}.log", error=install_root / f"install-{index}.stderr.log",
                    deadline=min(deadline, time.time() + step.get("timeout_seconds", 120)), cancelled=cancelled)
                if code:
                    raise ValueError(f"初始化步骤 {index} 失败")
            from .path_lint import require_portable
            require_portable(source)  # Initialization must not introduce absolute prompt paths.
            skills = install_root / "skills"
            skills.mkdir()
            for skill in manifest.get("skills", []):
                (skills / skill["name"]).symlink_to(within(source, skill["path"]), target_is_directory=True)
            (install_root / "python.txt").write_text(python)
            (install_root / "token").write_text(secrets.token_urlsafe(32))
            (install_root / "token").chmod(0o600)
            (install_root / "receipt.json").write_text(json.dumps({"installation_id": digest, "repo_id": repo_id,
                "commit": commit, "ref": ref, "namespace": "repo_" + repo_id, "manifest": manifest,
                **instruction_hashes(source)}, ensure_ascii=False, indent=2))
            self.ensure_service(digest)
            self.update(digest, status="ready", error=None)
        except BaseException as error:
            self.update(digest, status="failed", error=str(error))
            item = self.get(digest)
            if alive(item["pid"], item["birth"]):
                kill_tree(item["pid"], item["birth"])
            raise
        return self.get(digest)

    def health(self, item):
        if not item["service_alive"] or not item["endpoint"]:
            return None
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                response = client.get(item["endpoint"].removesuffix("/mcp") + "/health", headers=self.headers(item))
                if response.status_code == 200 and response.json().get("installation_id") == item["id"] and response.json().get("healthy"):
                    return response.json()
        except (httpx.HTTPError, ValueError, OSError):
            pass
        return None

    def headers(self, item, run_id=None):
        result = {"Authorization": "Bearer " + (Path(item["root"]) / "token").read_text().strip()}
        if run_id:
            result["X-Agent-Run"] = run_id
        return result

    def ensure_service(self, installation_id):
        item = self.get(installation_id)
        root = Path(item["root"])
        with installation_lock(root / "service.lock", 40):
            item = self.get(installation_id)
            if not item["enabled"]:
                raise ValueError("仓库服务已停用，请 repo start")
            if self.health(item):
                return item
            if item["service_alive"]:
                kill_tree(item["pid"], item["birth"])
            ready = root / "service.json"
            ready.unlink(missing_ok=True)
            env = runtime_environment(self.config.runtime, root / "service-home")
            with (root / "service.log").open("ab") as log:
                child = subprocess.Popen([sys.executable, "-m", "agent_base.repo_server", "--config", str(self.config.path),
                    "--installation", installation_id], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **spawn_options())
            birth = identity(child.pid)
            self.update(installation_id, pid=child.pid, birth=birth, endpoint=None, error=None)
            until = time.monotonic() + 30
            while time.monotonic() < until and alive(child.pid, birth):
                if ready.exists():
                    data = json.loads(ready.read_text())
                    self.update(installation_id, endpoint=data["endpoint"])
                    item = self.get(installation_id)
                    if self.health(item):
                        return item
                time.sleep(0.1)
            kill_tree(child.pid, birth)
            self.update(installation_id, error="MCP 常驻服务启动失败；见 service.log")
            raise ValueError("MCP 常驻服务启动失败；见 " + str(root / "service.log"))

    def stop(self, installation_id):
        item = self.get(installation_id)
        with installation_lock(Path(item["root"]) / "service.lock", 40):
            self.update(installation_id, enabled=0)
            kill_tree(item["pid"], item["birth"])
        return self.get(installation_id)

    def start(self, installation_id):
        self.update(installation_id, enabled=1, restart_attempts=0, next_retry=0)
        self.ensure_service(installation_id)
        return self.get(installation_id)

    def recover(self):
        for item in self.list():
            if item["status"] != "ready" or not item["enabled"]:
                continue
            if self.health(item):
                if item["restart_attempts"]:
                    self.update(item["id"], restart_attempts=0, next_retry=0)
                continue
            if item["restart_attempts"] >= 3 or time.time() < item["next_retry"]:
                continue
            self.update(item["id"], restart_attempts=item["restart_attempts"] + 1,
                        next_retry=time.time() + 30 * 2 ** item["restart_attempts"])
            try:
                self.ensure_service(item["id"])
            except (OSError, ValueError):
                pass  # After three failed attempts, require explicit start/repair.

    def bind(self, installation_id, job, workspace, *, reuse=False):
        item = self.ensure_service(installation_id)
        source = Path(item["root"]) / "source"
        if not workspace.exists():
            shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "resolved.json"), symlinks=True)
        resource_dir = workspace / ".agent"
        existing = resource_dir.exists() or resource_dir.is_symlink()
        if existing:
            catalog_file = resource_dir / "resources.json"
            if not reuse or not catalog_file.is_file() or json.loads(catalog_file.read_text()).get("installation_id") != installation_id:
                raise ValueError("仓库不能预置保留目录 .agent，或会话绑定的安装版本发生变化")
        else:
            resource_dir.mkdir()
            (workspace / ".agent-base-root").touch()
            (resource_dir / "skills").symlink_to(Path(item["root"]) / "skills", target_is_directory=True)
        catalog = {"namespace": item["namespace"], "installation_id": installation_id,
                   "skills": [{"name": s["name"], "path": f".agent/skills/{s['name']}/SKILL.md"} for s in item["manifest"].get("skills", [])],
                   "mcp": [{"id": s["id"], "transport": s["transport"]} for s in item["manifest"].get("mcp", {}).get("servers", [])]}
        (resource_dir / "resources.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
        package = Path(job["root"]) / "package"
        package.symlink_to(source, target_is_directory=True)
        (Path(job["root"]) / "interpreter.txt").write_text((Path(item["root"]) / "python.txt").read_text())
        (Path(job["root"]) / "receipt.json").write_text((Path(item["root"]) / "receipt.json").read_text())
        return item
