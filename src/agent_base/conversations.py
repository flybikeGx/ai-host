"""Durable web conversations over native CLI sessions, independent of HTTP streams."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from .config import PROJECT, validate_selection
from .engines import dsh_command, dsh_patch, parse_dsh_output, runtime_environment
from .package import within
from .processes import Cancelled, DeadlineExceeded, alive, identity as process_identity, kill_tree, run_process, spawn_options
from .repositories import Repositories, installation_lock
from .store import Store


BUSY = {"queued", "running"}


class ConversationBusy(ValueError):
    pass


class ChatError(Exception):
    """Page-surface error carrying the frozen §6.2 stable code."""

    status = 400
    code = "invalid_request"

    def __init__(self, detail: str, *, code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if code is not None:
            self.code = code


class ChatNotFound(ChatError):
    """404 must never reveal whether the object exists (§6.2)."""

    status = 404
    code = "conversation_not_found"


class ChatForbidden(ChatError):
    status = 403


class ChatInvalid(ChatError):
    status = 400


class ChatQuota(ChatError):
    """429 for per-user turn or stream quotas (§9, frozen codes)."""

    status = 429
    code = "user_turn_quota"


class StreamLimiter:
    """Per-process per-user SSE connection counter (§9).

    Multi-worker deployments need shared storage; ``agent-base serve`` is a
    single process for this phase, documented by C7.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}

    def acquire(self, key: str) -> bool:
        with self.lock:
            current = self.counts.get(key, 0)
            if current >= self.limit:
                return False
            self.counts[key] = current + 1
            return True

    def release(self, key: str) -> None:
        with self.lock:
            current = self.counts.get(key, 0)
            if current <= 1:
                self.counts.pop(key, None)
            else:
                self.counts[key] = current - 1

    def count(self, key: str) -> int:
        with self.lock:
            return self.counts.get(key, 0)


class Conversations:
    def __init__(self, config):
        self.config = config
        self.store = Store(config.data)
        self.repositories = Repositories(config)

    def targets(self, identity=None, *, default_domain_id=None):
        """Visible Agent catalog. ``identity=None`` is the trusted local caller path.

        HTTP always passes an identity: D6 keeps the Admin Agent to platform
        administrators, §7.4 filters repo Agents to the current domain and
        §8.2 treats a NULL installation domain as the default domain.
        """
        result = []
        if identity is None or identity.platform_admin or identity.is_bootstrap:
            result.append({"id": "admin", "name": "Admin Agent", "kind": "admin", "available": self.config.runtime.get("engine", "dsh") == "dsh",
                           "description": "安装、管理和调度所有仓库 Agent", "skills": [], "mcp": ["admin"]})
        for item in self.repositories.list():
            if item["status"] != "ready":
                continue
            if identity is not None:
                if identity.user_type == "service" or not identity.can("agent.read"):
                    continue
                if self._target_domain(item, default_domain_id) != identity.current_domain:
                    continue
            manifest = item["manifest"]
            result.append({"id": item["id"], "name": manifest.get("package", {}).get("name", item["repo_id"]), "kind": "repo",
                "description": manifest.get("task", {}).get("default", "已安装仓库 Agent"), "namespace": item["namespace"],
                "available": bool(item["enabled"]), "service_alive": item["service_alive"], "commit": item["commit_sha"],
                "skills": [s["name"] for s in manifest.get("skills", [])], "mcp": [s["id"] for s in manifest.get("mcp", {}).get("servers", [])]})
        return {"targets": result, "defaults": {"model": self.config.runtime["model"], "effort": self.config.runtime.get("effort", "high")}}

    @staticmethod
    def _target_domain(item, default_domain_id):
        return item.get("domain_id") or default_domain_id

    def create(self, target="admin", *, title=None, model=None, effort=None,
               identity=None, default_domain_id=None):
        """Create a conversation (§5.1 POST /api/conversations).

        HTTP callers pass an identity: ``agent.run`` is checked by the route,
        the target must be visible in ``targets()`` (otherwise 404) and the row
        is stamped with the caller's owner id and current domain (§7.5/§8.6).
        ``identity=None`` is the trusted local caller path and keeps the
        pre-governance NULL owner/domain behaviour.
        """
        if self.config.runtime.get("engine", "dsh") != "dsh":
            raise ValueError("网页连续对话目前使用 dsh；CodeAgent 会话适配尚未完成")
        selected = next((item for item in self.targets(identity, default_domain_id=default_domain_id)["targets"]
                         if item["id"] == target), None)
        if selected is None:
            if identity is not None:
                raise ChatNotFound("Agent 不存在", code="agent_not_found")
            raise ValueError("Agent 不存在或已停用，请先在管理页面启动资源")
        if not selected["available"]:
            if identity is not None:
                raise ChatInvalid("Agent 不存在或已停用，请先在管理页面启动资源", code="invalid_request")
            raise ValueError("Agent 不存在或已停用，请先在管理页面启动资源")
        runtime = {**self.config.runtime}
        if model:
            runtime["model"] = model
        if effort:
            runtime["effort"] = effort
        validate_selection(runtime)
        name = title.strip() if title else selected["name"] + " 对话"
        if not name or len(name) > 120:
            raise ValueError("对话名称须为 1–120 个字符")
        chat_id = uuid.uuid4().hex
        (self.config.data / "conversations" / chat_id).mkdir(parents=True, mode=0o700)
        spec = {"runtime": runtime, "auto_title": not bool(title), "agent_name": selected["name"]}
        now = time.time()
        owner = identity.user_id if identity is not None else None
        domain = identity.current_domain if identity is not None else None
        with self.store.connection() as conn:
            if identity is None:
                # Trusted local callers keep the pre-governance shape; the C2
                # migration columns exist only after create_app's startup hook.
                conn.execute("INSERT INTO conversations(id,title,target,spec,created,updated)"
                             " VALUES(?,?,?,?,?,?)",
                             (chat_id, name, target, json.dumps(spec), now, now))
            else:
                conn.execute("INSERT INTO conversations(id,title,target,spec,created,updated,owner_user_id,domain_id)"
                             " VALUES(?,?,?,?,?,?,?,?)",
                             (chat_id, name, target, json.dumps(spec), now, now, owner, domain))
        return self.get(chat_id)

    def get(self, chat_id):
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (chat_id,)).fetchone()
        if not row:
            raise ValueError("对话不存在")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        return result

    def turn(self, turn_id):
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM conversation_turns WHERE id=?", (turn_id,)).fetchone()
        if not row:
            raise ValueError("消息不存在")
        return dict(row)

    @staticmethod
    def public_turn(turn):
        return {key: turn.get(key) for key in ("id", "conversation_id", "user_text", "assistant_text", "status", "created", "updated", "error", "run_id")}

    def owned(self, chat_id, identity, *, scope_all=False):
        """The single ownership gate for every by-id read (§5.3, frozen).

        Missing, NULL-owned for non-bootstrap, other people's, and
        out-of-domain conversations all raise the same 404 (§6.2).
        ``scope_all`` is the explicit, read-only platform-admin bypass; callers
        audit it and must never pass it on write routes.
        """
        try:
            row = self.get(chat_id)
        except ValueError:
            raise ChatNotFound("对话不存在")
        if row.get("owner_user_id") is None and not identity.is_bootstrap:
            raise ChatNotFound("对话不存在")
        if scope_all:
            if not identity.platform_admin and not identity.is_bootstrap:
                raise ChatForbidden("只有平台管理员可以使用只读范围", code="scope_denied")
            return row
        if identity.is_bootstrap:
            return row
        if row.get("owner_user_id") != identity.user_id:
            raise ChatNotFound("对话不存在")
        if row.get("domain_id") not in identity.domains and row.get("domain_id") is not None:
            raise ChatNotFound("对话不存在")
        return row

    def list(self, *, archived=False, identity=None, domain_id=None, scope_all=False,
             default_domain_id=None):
        """Owner- and domain-scoped conversation list (§5.1/§4).

        ``identity=None`` is the trusted local caller path. The ownership
        condition exists in exactly one place here; bootstrap keeps its
        account-free semantics and never filters by owner (§3.3).
        """
        self.reconcile()
        clause, values = ["archived=?"], [int(archived)]
        if identity is not None:
            if not scope_all and not identity.is_bootstrap:
                clause.append("owner_user_id=?")
                values.append(identity.user_id)
            domains = sorted(identity.domains)
            if domain_id is not None:
                clause.append("COALESCE(domain_id, ?)=?")
                values.extend([default_domain_id, domain_id])
            elif domains:
                clause.append("COALESCE(domain_id, ?) IN (" + ",".join("?" for _ in domains) + ")")
                values.extend([default_domain_id, *domains])
            else:
                return []
        sql = "SELECT id FROM conversations WHERE " + " AND ".join(clause) + " ORDER BY updated DESC LIMIT 200"
        with self.store.connection() as conn:
            ids = [r[0] for r in conn.execute(sql, values)]
        result = []
        for chat_id in ids:
            item = self.get(chat_id)
            with self.store.connection() as conn:
                latest = conn.execute("SELECT status,user_text FROM conversation_turns WHERE conversation_id=? ORDER BY created DESC LIMIT 1", (chat_id,)).fetchone()
            item["status"] = latest["status"] if latest else "idle"
            item["preview"] = latest["user_text"][:80] if latest else "还没有消息"
            result.append(item)
        return result

    def snapshot(self, chat_id, identity=None, *, scope_all=False):
        self.reconcile()
        if identity is not None:
            self.owned(chat_id, identity, scope_all=scope_all)
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            chat = dict(conn.execute("SELECT * FROM conversations WHERE id=?", (chat_id,)).fetchone())
            turns = [dict(r) for r in conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY created DESC LIMIT 100", (chat_id,))][::-1]
            events = [dict(r) for r in conn.execute("SELECT * FROM conversation_events WHERE conversation_id=? ORDER BY seq DESC LIMIT 5000", (chat_id,))][::-1]
            cursor = conn.execute("SELECT coalesce(max(seq),0) FROM conversation_events WHERE conversation_id=?", (chat_id,)).fetchone()[0]
        chat["spec"] = json.loads(chat["spec"])
        include_files = identity is None or identity.can("file.read")  # §5.1: task.read alone must not expose artifacts
        return {"conversation": chat, "turns": [self.public_turn(t) for t in turns],
                "events": [{**e, "data": json.loads(e["data"])} for e in events], "cursor": cursor,
                "files": self.files(chat_id, identity, scope_all=scope_all) if include_files else []}

    def files(self, chat_id, identity=None, *, scope_all=False):
        if identity is not None:
            self.owned(chat_id, identity, scope_all=scope_all)
        else:
            self.get(chat_id)
        workspace = self.config.data / "conversations" / chat_id / "workspace"
        directory = workspace / "artifacts"
        if not directory.is_dir():
            return []
        result = []
        for file in sorted(directory.rglob("*")):
            if file.is_file() and not file.is_symlink():
                relative = file.relative_to(workspace).as_posix()
                try:
                    within(workspace, relative)
                except ValueError:
                    continue
                result.append({"path": relative, "size": file.stat().st_size})
                if len(result) >= 100:
                    break
        return result

    def artifact(self, chat_id, relative, identity=None, *, scope_all=False):
        if identity is not None:
            self.owned(chat_id, identity, scope_all=scope_all)
        else:
            self.get(chat_id)
        if not isinstance(relative, str) or not relative.startswith("artifacts/"):
            raise ValueError("只允许下载本会话 artifacts 中的文件")
        workspace = self.config.data / "conversations" / chat_id / "workspace"
        try:
            file = within(workspace, relative)
        except ValueError as error:
            if str(error).startswith("文件不存在"):
                raise ChatNotFound("文件不存在", code="artifact_not_found")
            raise
        if not file.is_file():
            raise ChatNotFound("文件不存在", code="artifact_not_found")
        if file.stat().st_size > 64 * 1024 * 1024:
            raise ChatInvalid("文件超过 64 MiB")
        return file

    def rename(self, chat_id, *, identity=None, title=None, archived=None):
        if identity is not None:
            self.owned(chat_id, identity)  # owner only; never scope_all on writes
        else:
            self.get(chat_id)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if archived and conn.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND status IN ('queued','running')", (chat_id,)).fetchone():
                raise ConversationBusy("请先停止或等待本轮结束后再归档")
            if title is not None:
                if not title.strip() or len(title) > 120:
                    raise ValueError("对话名称须为 1–120 个字符")
                conn.execute("UPDATE conversations SET title=?,updated=? WHERE id=?", (title.strip(), time.time(), chat_id))
            if archived is not None:
                conn.execute("UPDATE conversations SET archived=?,updated=? WHERE id=?", (int(archived), time.time(), chat_id))
        return self.get(chat_id)

    def send(self, chat_id, text, client_key, identity=None):
        """Accept one turn after ownership, lifecycle and quota checks.

        The per-user active-turn count and the insert share one
        ``BEGIN IMMEDIATE`` transaction (§9) so concurrent requests cannot
        both pass the limit. The global 32-turn cap stays as the 409 fallback.
        """
        if identity is not None:
            chat = self.owned(chat_id, identity)  # object 404 before body 400
        else:
            chat = self.get(chat_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 64 * 1024:
            raise ValueError("消息须为非空文本且不超过 64 KiB")
        if not isinstance(client_key, str) or not 1 <= len(client_key) <= 200:
            raise ValueError("请为本次消息提供有效 client_key")
        self.reconcile()
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND client_key=?", (chat_id, client_key)).fetchone()
            if existing:
                if existing["user_text"] != text:
                    raise ValueError("同一 client_key 不能发送不同内容")
                return {**self.public_turn(dict(existing)), "duplicate": True}
            if chat["archived"]:
                raise ChatInvalid("请先恢复已归档对话", code="conversation_archived")
            if conn.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND status IN ('queued','running')", (chat_id,)).fetchone():
                raise ConversationBusy("本对话还有一轮正在处理，请等待或停止后再发送")
            if identity is not None:
                # §9: a user's active turns aggregate over conversations they
                # own; trusted local callers have no user bucket.
                active = conn.execute(
                    "SELECT count(*) FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id"
                    " WHERE c.owner_user_id IS ? AND t.status IN ('queued','running')",
                    (chat.get("owner_user_id"),)).fetchone()[0]
                if active >= self.config.per_user_max_turns:
                    raise ChatQuota("已达到每用户同时进行的对话上限，请先停止其他对话", code="user_turn_quota")
            if conn.execute("SELECT count(*) FROM conversation_turns WHERE status IN ('queued','running')").fetchone()[0] >= 32:
                raise ConversationBusy("对话队列已满，请稍后重试")
            turn_id = uuid.uuid4().hex
            root = self.config.data / "conversations" / chat_id / "turns" / turn_id
            root.mkdir(parents=True, mode=0o700)
            run_id = None
            if chat["target"] != "admin":
                spec = {"installation_id": chat["target"], "runtime": chat["spec"]["runtime"], "conversation_id": chat_id,
                        "user_id": chat.get("owner_user_id"), "domain_id": chat.get("domain_id"),
                        "max_concurrent": self.config.max_concurrent, "kind": "conversation"}
                run_id = self.store.insert(conn, spec)
            now = time.time()
            conn.execute("INSERT INTO conversation_turns(id,conversation_id,client_key,user_text,status,created,updated,run_id,root) VALUES(?,?,?,?,'queued',?,?,?,?)",
                         (turn_id, chat_id, client_key, text, now, now, run_id, str(root)))
            if chat["spec"].get("auto_title") and not conn.execute("SELECT 1 FROM conversation_turns WHERE conversation_id=? AND id!=?", (chat_id, turn_id)).fetchone():
                conn.execute("UPDATE conversations SET title=? WHERE id=?", (text.strip().splitlines()[0][:40], chat_id))
            conn.execute("UPDATE conversations SET updated=? WHERE id=?", (now, chat_id))
            self._event(conn, chat_id, turn_id, "turn.queued", self.public_turn({"id": turn_id, "conversation_id": chat_id, "user_text": text,
                "assistant_text": "", "status": "queued", "created": now, "updated": now, "run_id": run_id}))
        env = runtime_environment(chat["spec"]["runtime"], self.config.data / "conversations" / chat_id / "dsh-home")
        try:
            with (root / "supervisor.log").open("ab") as log:
                child = subprocess.Popen([sys.executable, "-m", "agent_base", "--config", str(self.config.path), "_chat_execute", turn_id],
                    env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **spawn_options())
            self.update_turn(turn_id, pid=child.pid, birth=process_identity(child.pid))
        except OSError as error:
            self.finish(turn_id, "failed", str(error))
        return {**self.public_turn(self.turn(turn_id)), "duplicate": False}

    def _event(self, conn, chat_id, turn_id, kind, data):
        conn.execute("INSERT INTO conversation_events(conversation_id,turn_id,at,type,data) VALUES(?,?,?,?,?)",
                     (chat_id, turn_id, time.time(), kind, json.dumps(data, ensure_ascii=False)))

    def event(self, turn_id, kind, data):
        turn = self.turn(turn_id)
        with self.store.connection() as conn:
            self._event(conn, turn["conversation_id"], turn_id, kind, data)
            if kind == "text.delta":
                conn.execute("UPDATE conversation_turns SET assistant_text=assistant_text || ?,updated=? WHERE id=? AND status='running'", (data["text"], time.time(), turn_id))

    def events(self, chat_id, after=0, identity=None, *, scope_all=False):
        if identity is not None:
            self.owned(chat_id, identity, scope_all=scope_all)
        else:
            self.get(chat_id)
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM conversation_events WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT 200", (chat_id, after)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def update_turn(self, turn_id, **fields):
        if set(fields) - {"status", "pid", "birth", "child_pid", "child_birth", "cancel_requested"}:
            raise ValueError("无效消息状态字段")
        fields["updated"] = time.time()
        with self.store.connection() as conn:
            conn.execute(f"UPDATE conversation_turns SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), turn_id))

    def finish(self, turn_id, status, error=None, text=None):
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn = conn.execute("SELECT * FROM conversation_turns WHERE id=?", (turn_id,)).fetchone()
            if not turn or turn["status"] not in BUSY:
                return
            answer = text if text is not None else turn["assistant_text"]
            conn.execute("UPDATE conversation_turns SET status=?,error=?,assistant_text=?,updated=?,child_pid=NULL,child_birth=NULL WHERE id=?",
                         (status, error, answer, time.time(), turn_id))
            conn.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), turn["conversation_id"]))
            self._event(conn, turn["conversation_id"], turn_id, "turn.finished", {"status": status, "error": error, "assistant_text": answer, "run_id": turn["run_id"]})
        if turn["run_id"]:
            self.store.update(turn["run_id"], status=status, error=error, session_status="finished" if status == "succeeded" else status,
                              result={"kind": "conversation", "text": answer, "verification": "conversation_only"}, process_pid=None, process_birth=None)

    def cancel(self, chat_id, identity=None):
        if identity is not None:
            self.owned(chat_id, identity)  # owner only; no administrator write bypass
        else:
            self.get(chat_id)
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM conversation_turns WHERE conversation_id=? AND status IN ('queued','running')", (chat_id,)).fetchone()
        if not row:
            return {"requested": False}
        self.update_turn(row["id"], cancel_requested=1)
        self.event(row["id"], "turn.stopping", {})
        return {"requested": True, "turn_id": row["id"]}

    def reconcile(self):
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM conversation_turns WHERE status IN ('queued','running') AND updated<?", (time.time() - 3,)).fetchall()
        for row in rows:
            if alive(row["pid"], row["birth"]):
                continue
            # Finish uses a transaction and refuses to overwrite newer terminal states.
            if alive(row["child_pid"], row["child_birth"]):
                kill_tree(row["child_pid"], row["child_birth"])
            self.finish(row["id"], "interrupted", "对话进程已退出；保留已生成内容，不自动重发消息。")

    def execute(self, turn_id):
        with self.store.connection() as conn:
            changed = conn.execute("UPDATE conversation_turns SET executor_started=1,pid=?,birth=?,updated=? WHERE id=? AND status='queued' AND executor_started=0",
                (os.getpid(), process_identity(os.getpid()), time.time(), turn_id)).rowcount
        if not changed:
            return
        turn = self.turn(turn_id)
        chat = self.get(turn["conversation_id"])
        home = self.config.data / "conversations" / chat["id"]
        root = Path(turn["root"])
        runtime = chat["spec"]["runtime"]
        workspace = home / "workspace"
        run_id = turn["run_id"]
        cancel = lambda: bool(self.turn(turn_id)["cancel_requested"] or (run_id and self.store.get(run_id)["cancel_requested"]))
        signal.signal(signal.SIGTERM, lambda *_: self.update_turn(turn_id, cancel_requested=1))
        signal.signal(signal.SIGINT, lambda *_: self.update_turn(turn_id, cancel_requested=1))
        if run_id:
            with self.store.connection() as conn:
                conn.execute("UPDATE runs SET executor_started=1,pid=?,birth=?,updated=? WHERE id=?", (os.getpid(), process_identity(os.getpid()), time.time(), run_id))
        try:
            with installation_lock(home / "session.lock", 5):
                if run_id:
                    while not self.store.claim(run_id, self.config.max_concurrent):
                        if cancel():
                            raise Cancelled("消息已停止")
                        if self.store.get(run_id)["status"] != "queued":
                            raise RuntimeError("对话运行已中断")
                        from .service import Service
                        Service(self.config).reconcile()
                        self.update_turn(turn_id, pid=os.getpid(), birth=process_identity(os.getpid()))
                        self.store.update(run_id, pid=os.getpid(), birth=process_identity(os.getpid()))
                        time.sleep(0.2)
                if cancel():
                    raise Cancelled("消息已停止")
                deadline = time.time() + runtime.get("timeout_seconds", 1800)
                self.update_turn(turn_id, status="running")
                self.event(turn_id, "turn.started", {"status": "running"})
                env = runtime_environment(runtime, home / "dsh-home")
                if chat["target"] == "admin":
                    workspace.mkdir(exist_ok=True)
                    (workspace / ".agent-base-root").touch()
                    shutil.copyfile(PROJECT / "AGENTS.md", workspace / "AGENTS.md")
                    prompt = (PROJECT / "AGENTS.md").read_text() + "\n\n用户本轮消息：\n" + turn["user_text"]
                    patch = dsh_patch(runtime, root, admin_config=self.config.path)
                else:
                    job = self.store.get(run_id)
                    self.store.update(run_id, workspace=str(workspace), started_at=time.time(), deadline=deadline, session_status="starting")
                    item = self.repositories.bind(chat["target"], job, workspace, reuse=True)
                    from .repo_runtime import repo_patch, repo_prompt
                    prompt = repo_prompt(Path(item["root"]) / "source", workspace, item["manifest"], turn["user_text"])
                    prompt = prompt.replace("的一次独立任务", "的多轮对话")
                    prompt += "\n这是可继续追问的对话，使用本会话先前的消息。只处理本轮请求；普通问答无需重复执行默认任务或生成产物。\n"
                    patch = repo_patch(runtime, root, self.repositories, chat["target"], run_id, env)
                    if item["manifest"].get("schema_version") == 3:
                        from .service_resolver import ServiceResolver
                        prompt = ServiceResolver(self.config).attach(
                            patch=patch, env=env, workspace=workspace, manifest=item["manifest"],
                            installation=item, turn_id=turn_id, deadline=deadline) + prompt
                    self.store.update(run_id, status="running")
                from .knowledge_service import attach
                actor = 'admin' if chat['target'] == 'admin' else 'repo:' + item['repo_id']
                prompt = attach(self.config, patch, env, actor, {'turn_id': turn_id, 'run_id': run_id, 'installation_id': chat['target']}, deadline, workspace) + prompt
                prompt_file = root / "prompt.txt"
                prompt_file.write_text(prompt, encoding="utf-8")
                output = root / "runtime.jsonl"
                cursor = 0
                heartbeat = 0

                def observe():
                    nonlocal cursor, heartbeat
                    if time.time() - heartbeat > 1:
                        self.update_turn(turn_id, pid=os.getpid(), birth=process_identity(os.getpid()))
                        if run_id:
                            self.store.update(run_id, heartbeat=time.time())
                        heartbeat = time.time()
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
                            self.native_event(turn_id, chat["id"], run_id, event)

                def started(pid, birth):
                    self.update_turn(turn_id, child_pid=pid, child_birth=birth)
                    if run_id:
                        self.store.update(run_id, process_pid=pid, process_birth=birth)

                code = run_process(dsh_command(runtime, patch, session_id=chat["native_id"]), cwd=workspace, env=env,
                    input_file=prompt_file, output=output, error=root / "runtime.stderr.log", deadline=deadline, cancelled=cancel,
                    on_started=started, on_poll=observe)
                observe()
                if code:
                    raise RuntimeError(f"CLI 退出码 {code}，本轮日志已保留")
                result = parse_dsh_output(output)
                if chat["native_id"] and result["session_id"] != chat["native_id"]:
                    raise RuntimeError("CLI 返回的 session 与当前对话不一致")
                if cancel():
                    raise Cancelled("本轮已停止")
                self.finish(turn_id, "succeeded", text=result["text"])
        except BaseException as error:
            traceback.print_exc()
            status = "cancelled" if isinstance(error, Cancelled) else "timed_out" if isinstance(error, DeadlineExceeded) else "failed"
            self.finish(turn_id, status, str(error))

    def native_event(self, turn_id, chat_id, run_id, event):
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "session" and isinstance(event.get("sessionId"), str):
            native_id = event["sessionId"]
            with self.store.connection() as conn:
                old = conn.execute("SELECT native_id FROM conversations WHERE id=?", (chat_id,)).fetchone()[0]
                if old and old != native_id:
                    raise RuntimeError("CLI 没有续接当前对话 session")
                conn.execute("UPDATE conversations SET native_id=? WHERE id=?", (native_id, chat_id))
            if run_id:
                self.store.update(run_id, session_id=native_id, session_status="running")
            self.event(turn_id, "session.opened", {"session_id": native_id})
        elif kind == "text" and isinstance(event.get("text"), str):
            self.event(turn_id, "text.delta", {"text": event["text"]})
        elif kind == "tool_call":
            self.event(turn_id, "tool.call", {"call_id": event.get("callId"), "tool": event.get("tool", "tool"), "input": self.bounded(event.get("input"))})
        elif kind == "tool_result":
            result = event.get("result")
            parsed = result
            if isinstance(result, str):
                try:
                    parsed = json.loads(result)
                except ValueError:
                    pass
            status = "failed" if isinstance(parsed, dict) and parsed.get("isError") else event.get("status", "unknown")
            self.event(turn_id, "tool.result", {"call_id": event.get("callId"), "status": status, "result": self.bounded(result)})
        elif kind == "status":
            self.event(turn_id, "progress", {"phase": event.get("phase", "working")})
        # Do not persist or display native thinking/reasoning text in the web UI.

    @staticmethod
    def bounded(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 12000 else text[:12000] + "\n…（内容已截断）"
