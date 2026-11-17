"""Managed shared-service persistence: definitions, revisions, deployments,
operations, agent identities, bindings, incidents and access grants.

The tables live in the same runs.sqlite transaction store; schema evolution is
recorded in platform_migrations and every step stays backward compatible with
existing v1/v2 data.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .store import Store

SERVICE_SCHEMA_VERSION = 2
DEPLOYMENT_STATES = {"running", "stopped", "removed"}
PUBLISH_STATES = {"unpublished", "published", "withdrawn"}
OBSERVED_STATES = {"unknown", "starting", "healthy", "degraded", "unhealthy", "stopped"}
VERIFICATION_LEVELS = {"not_verified", "protocol_verified", "business_verified"}
OPERATION_ACTIVE = {"queued", "preparing", "installing", "validating", "recovering"}
OPERATION_TERMINAL = {"succeeded", "failed", "cancelled", "needs_input"}
INCIDENT_STATES = {"open", "monitoring", "closed"}


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ServiceStore:
    """Versioned schema plus typed helpers for managed service records."""

    def __init__(self, data: Path):
        self.data = Path(data)
        self.db = self.data / "runs.sqlite"
        self.migrate()

    # -- schema ---------------------------------------------------------------
    def migrate(self):
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS platform_migrations(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
            """)
            applied = {row[0] for row in conn.execute("SELECT version FROM platform_migrations")}
            if SERVICE_SCHEMA_VERSION not in applied:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS services(
                        id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                        created REAL NOT NULL, updated REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS service_revisions(
                        id TEXT PRIMARY KEY, service_id TEXT NOT NULL,
                        origin TEXT NOT NULL, commit_sha TEXT NOT NULL, component TEXT NOT NULL DEFAULT '',
                        manifest TEXT NOT NULL, manifest_digest TEXT NOT NULL,
                        root TEXT NOT NULL, python TEXT NOT NULL, created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS deployments(
                        id TEXT PRIMARY KEY, service_id TEXT NOT NULL, revision_id TEXT,
                        mode TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'stopped',
                        publish_state TEXT NOT NULL DEFAULT 'unpublished',
                        observed_state TEXT NOT NULL DEFAULT 'unknown', observed_at REAL, observed_evidence TEXT,
                        verification TEXT NOT NULL DEFAULT 'not_verified', endpoint TEXT,
                        gateway_port INTEGER, gateway_pid INTEGER, gateway_birth REAL,
                        generation INTEGER NOT NULL DEFAULT 0, config TEXT NOT NULL DEFAULT '{}',
                        config_revision INTEGER NOT NULL DEFAULT 1,
                        recovery_attempts INTEGER NOT NULL DEFAULT 0, recovery_window_start REAL, next_retry_at REAL,
                        probe_failures INTEGER NOT NULL DEFAULT 0, probe_successes INTEGER NOT NULL DEFAULT 0,
                        maintenance INTEGER NOT NULL DEFAULT 0,
                        created REAL NOT NULL, updated REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS service_operations(
                        id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, kind TEXT NOT NULL,
                        status TEXT NOT NULL, idempotency_key TEXT,
                        plan TEXT NOT NULL DEFAULT '{}', error TEXT,
                        pid INTEGER, birth REAL,
                        created REAL NOT NULL, updated REAL NOT NULL, finished REAL);
                    CREATE INDEX IF NOT EXISTS service_operations_deployment ON service_operations(deployment_id, created);
                    CREATE TABLE IF NOT EXISTS agent_identities(
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'repo',
                        created REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS agent_identity_sources(
                        source TEXT PRIMARY KEY, agent_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS service_bindings(
                        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, deployment_id TEXT NOT NULL,
                        alias TEXT NOT NULL, service_id TEXT NOT NULL,
                        contract TEXT NOT NULL DEFAULT '', tools TEXT NOT NULL DEFAULT '[]',
                        scope TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created REAL NOT NULL, updated REAL NOT NULL,
                        UNIQUE(agent_id, deployment_id, alias));
                    CREATE INDEX IF NOT EXISTS service_bindings_deployment ON service_bindings(deployment_id);
                    CREATE TABLE IF NOT EXISTS service_incidents(
                        id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, generation INTEGER,
                        status TEXT NOT NULL DEFAULT 'open', summary TEXT NOT NULL DEFAULT '',
                        affected_agents TEXT NOT NULL DEFAULT '[]', actions TEXT NOT NULL DEFAULT '[]',
                        opened_at REAL NOT NULL, updated_at REAL NOT NULL, closed_at REAL, close_evidence TEXT);
                    CREATE INDEX IF NOT EXISTS service_incidents_deployment ON service_incidents(deployment_id, status);
                    CREATE TABLE IF NOT EXISTS service_health_events(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL, at REAL NOT NULL,
                        level TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}');
                    CREATE INDEX IF NOT EXISTS service_health_events_deployment ON service_health_events(deployment_id, seq);
                    CREATE TABLE IF NOT EXISTS service_grants(
                        hash TEXT PRIMARY KEY, definition TEXT NOT NULL, expires REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS platform_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """)
                conn.execute("INSERT OR REPLACE INTO platform_migrations VALUES(?,?)", (SERVICE_SCHEMA_VERSION, time.time()))
            # Defensive completion for databases created mid-migration.
            for name in ("services", "service_revisions", "deployments", "service_operations", "agent_identities",
                         "agent_identity_sources", "service_bindings", "service_incidents", "service_health_events",
                         "service_grants", "platform_settings"):
                conn.execute(f"SELECT 1 FROM {name} LIMIT 1")

    @contextmanager
    def connection(self, *, write: bool = False):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        if write:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- settings -------------------------------------------------------------
    def setting(self, key: str, default=None):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM platform_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def save_setting(self, key: str, value):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO platform_settings VALUES(?,?)", (key, encoded(value)))

    # -- services / revisions -------------------------------------------------
    def service(self, service_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        return dict(row) if row else None

    def save_service(self, service_id: str, *, title: str = "") -> dict:
        now = time.time()
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO services VALUES(?,?,?,?)", (service_id, title, now, now))
            conn.execute("UPDATE services SET updated=?, title=CASE WHEN ?<>'' THEN ? ELSE title END WHERE id=?",
                         (now, title, title, service_id))
        return self.service(service_id)

    def revision(self, revision_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM service_revisions WHERE id=?", (revision_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["manifest"] = json.loads(item["manifest"])
        return item

    def save_revision(self, *, service_id: str, origin: str, commit: str, component: str,
                      manifest: dict, root: Path, python: str) -> dict:
        revision_id = digest(encoded({"service": service_id, "origin": origin, "commit": commit,
                                      "component": component, "manifest": manifest}))[:24]
        now = time.time()
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO service_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (revision_id, service_id, origin, commit, component, encoded(manifest),
                          digest(encoded(manifest)), str(root), python, now))
        return self.revision(revision_id)

    # -- deployments ----------------------------------------------------------
    def deployment(self, deployment_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["config"] = json.loads(item["config"])
        return item

    def deployments(self) -> list[dict]:
        with self.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM deployments ORDER BY created")]
        return [item for item in (self.deployment(i) for i in ids) if item]

    def create_deployment(self, deployment_id: str, *, service_id: str, mode: str, config: dict,
                          revision_id: str | None = None, endpoint: str | None = None,
                          gateway_port: int | None = None) -> dict:
        if not isinstance(deployment_id, str) or not _valid_slug(deployment_id):
            raise ValueError("部署名须为 1–64 位小写字母、数字或连字符（字母开头）")
        if mode not in {"managed", "external"}:
            raise ValueError("部署模式必须是 managed 或 external")
        now = time.time()
        with self.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM deployments WHERE id=?", (deployment_id,)).fetchone():
                raise ValueError(f"部署已存在：{deployment_id}")
            conn.execute("INSERT INTO deployments(id,service_id,revision_id,mode,config,endpoint,gateway_port,created,updated)"
                         " VALUES(?,?,?,?,?,?,?,?,?)",
                         (deployment_id, service_id, revision_id, mode, encoded(config), endpoint, gateway_port, now, now))
        self.save_service(service_id)
        return self.deployment(deployment_id)

    def update_deployment(self, deployment_id: str, **fields):
        allowed = {"revision_id", "desired_state", "publish_state", "observed_state", "observed_at", "observed_evidence",
                   "verification", "endpoint", "gateway_port", "gateway_pid", "gateway_birth", "generation", "config",
                   "config_revision", "recovery_attempts", "recovery_window_start", "next_retry_at",
                   "probe_failures", "probe_successes", "maintenance"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"无效部署字段：{sorted(set(fields) - allowed)}")
        if "config" in fields:
            fields["config"] = encoded(fields["config"])
        fields["updated"] = time.time()
        with self.connection() as conn:
            changed = conn.execute(f"UPDATE deployments SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                                   (*fields.values(), deployment_id)).rowcount
        if not changed:
            raise ValueError(f"部署不存在：{deployment_id}")

    def delete_deployment(self, deployment_id: str):
        with self.connection(write=True) as conn:
            if not conn.execute("SELECT 1 FROM deployments WHERE id=?", (deployment_id,)).fetchone():
                raise ValueError(f"部署不存在：{deployment_id}")
            bindings = conn.execute("SELECT agent_id FROM service_bindings WHERE deployment_id=? AND enabled=1",
                                    (deployment_id,)).fetchall()
            if bindings:
                raise ValueError("部署仍有启用的绑定，先解绑或停用后再删除")
            conn.execute("DELETE FROM service_bindings WHERE deployment_id=?", (deployment_id,))
            conn.execute("UPDATE deployments SET desired_state='removed' WHERE id=?", (deployment_id,))

    # -- operations -----------------------------------------------------------
    def operation(self, operation_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM service_operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["plan"] = json.loads(item["plan"]) if item["plan"] else {}
        return item

    def operations(self, deployment_id: str | None = None, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            if deployment_id:
                rows = conn.execute("SELECT id FROM service_operations WHERE deployment_id=? ORDER BY created DESC LIMIT ?",
                                    (deployment_id, limit)).fetchall()
            else:
                rows = conn.execute("SELECT id FROM service_operations ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [item for item in (self.operation(r[0]) for r in rows) if item]

    def active_install(self, deployment_id: str, *, operation_id: str | None = None) -> dict | None:
        states = sorted(OPERATION_ACTIVE)
        query = ("SELECT id FROM service_operations WHERE deployment_id=? AND kind='install'"
                 f" AND status IN ({','.join('?' for _ in states)})")
        args = [deployment_id, *states]
        if operation_id is not None:
            query += " AND id<>?"
            args.append(operation_id)
        with self.connection() as conn:
            row = conn.execute(query + " ORDER BY created LIMIT 1", args).fetchone()
        return self.operation(row["id"]) if row else None

    def create_operation(self, *, deployment_id: str, kind: str, plan: dict | None = None,
                         idempotency_key: str | None = None, status: str = "queued") -> dict:
        now = time.time()
        operation_id = "op_" + uuid.uuid4().hex[:20]
        with self.connection(write=True) as conn:
            if idempotency_key:
                existing = conn.execute("SELECT id FROM service_operations WHERE deployment_id=? AND kind=? AND idempotency_key=?",
                                        (deployment_id, kind, idempotency_key)).fetchone()
                if existing:
                    item = self.operation(existing[0])
                    item["duplicate"] = True
                    return item
            if kind == "install" and status in OPERATION_ACTIVE:
                # BEGIN IMMEDIATE serializes admission across processes. Check
                # and reserve in this same transaction, after idempotent lookup:
                # retries reuse their operation even if another install is active.
                states = sorted(OPERATION_ACTIVE)
                active = conn.execute(
                    "SELECT id,status FROM service_operations WHERE deployment_id=? AND kind='install'"
                    f" AND status IN ({','.join('?' for _ in states)}) LIMIT 1",
                    (deployment_id, *states)).fetchone()
                if active:
                    raise ValueError(f"部署已有进行中的安装操作 {active['id']}（{active['status']}）；"
                                     "请等待完成或取消后再提交新的安装")
            conn.execute("INSERT INTO service_operations(id,deployment_id,kind,status,idempotency_key,plan,created,updated)"
                         " VALUES(?,?,?,?,?,?,?,?)",
                         (operation_id, deployment_id, kind, status, idempotency_key, encoded(plan or {}), now, now))
        return self.operation(operation_id)

    def update_operation(self, operation_id: str, **fields):
        allowed = {"status", "plan", "error", "pid", "birth"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效操作字段")
        if "plan" in fields:
            fields["plan"] = encoded(fields["plan"])
        fields["updated"] = time.time()
        if fields.get("status") in OPERATION_TERMINAL:
            fields["finished"] = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT status FROM service_operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise ValueError(f"操作不存在：{operation_id}")
            if "status" in fields and row["status"] in OPERATION_TERMINAL and fields["status"] != row["status"]:
                raise ValueError("操作已达终态，不能再次变更状态")
            conn.execute(f"UPDATE service_operations SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                         (*fields.values(), operation_id))

    def cancel_requested(self, operation_id: str) -> bool:
        item = self.operation(operation_id)
        return bool(item and item["status"] == "cancelled")

    # -- agent identities -----------------------------------------------------
    def agent_identity(self, agent_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM agent_identities WHERE id=?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def agent_identity_for_source(self, source: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT a.* FROM agent_identities a JOIN agent_identity_sources s ON s.agent_id=a.id"
                               " WHERE s.source=?", (source,)).fetchone()
        return dict(row) if row else None

    def ensure_agent_identity(self, *, source: str | None = None, name: str, kind: str = "repo",
                              agent_id: str | None = None) -> dict:
        """Register (or return) the stable platform identity for one caller."""
        if agent_id == "admin" or (source == "admin"):
            existing = self.agent_identity("admin")
            if existing:
                return existing
            with self.connection() as conn:
                conn.execute("INSERT OR IGNORE INTO agent_identities VALUES(?,?,?,?)",
                             ("admin", name or "Admin", "builtin", time.time()))
                conn.execute("INSERT OR IGNORE INTO agent_identity_sources VALUES(?,?)", ("admin", "admin"))
            return self.agent_identity("admin")
        if source:
            existing = self.agent_identity_for_source(source)
            if existing:
                return existing
        agent_id = agent_id or ("agt_" + uuid.uuid4().hex[:16])
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO agent_identities VALUES(?,?,?,?)", (agent_id, name, kind, time.time()))
            if source:
                conn.execute("INSERT OR REPLACE INTO agent_identity_sources VALUES(?,?)", (source, agent_id))
        return self.agent_identity(agent_id)

    def agent_identities(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT a.*, group_concat(s.source) AS sources FROM agent_identities a"
                                " LEFT JOIN agent_identity_sources s ON s.agent_id=a.id GROUP BY a.id ORDER BY a.created").fetchall()
        return [dict(row) for row in rows]

    # -- bindings -------------------------------------------------------------
    def binding(self, binding_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM service_bindings WHERE id=?", (binding_id,)).fetchone()
        return _binding(row) if row else None

    def bindings(self, *, agent_id: str | None = None, deployment_id: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM service_bindings WHERE 1=1", []
        if agent_id:
            query += " AND agent_id=?"
            args.append(agent_id)
        if deployment_id:
            query += " AND deployment_id=?"
            args.append(deployment_id)
        with self.connection() as conn:
            rows = conn.execute(query + " ORDER BY created", args).fetchall()
        return [_binding(row) for row in rows]

    def binding_for(self, agent_id: str, alias: str, service_id: str) -> dict | None:
        """One enabled binding matching the agent's declared alias and service."""
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM service_bindings WHERE agent_id=? AND alias=? AND service_id=? AND enabled=1"
                                " ORDER BY updated DESC", (agent_id, alias, service_id)).fetchall()
        return _binding(rows[0]) if rows else None

    def save_binding(self, *, agent_id: str, deployment_id: str, alias: str, service_id: str,
                     contract: str, tools: list, scope: dict | None = None, enabled: bool = True,
                     expected_revision: int | None = None) -> dict:
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
            raise ValueError("绑定工具必须是非空字符串数组")
        if len(set(tools)) != len(tools):
            raise ValueError("绑定工具不能重复")
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("绑定别名不能为空")
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM service_bindings WHERE agent_id=? AND deployment_id=? AND alias=?",
                               (agent_id, deployment_id, alias)).fetchone()
            if row:
                if expected_revision is not None and row["revision"] != expected_revision:
                    raise ValueError("绑定已变化，请刷新后重试")
                conn.execute("UPDATE service_bindings SET service_id=?,contract=?,tools=?,scope=?,enabled=?,revision=revision+1,"
                             "updated=? WHERE id=?",
                             (service_id, contract, encoded(tools), encoded(scope or {}), int(enabled), now, row["id"]))
                binding_id = row["id"]
            else:
                if expected_revision is not None:
                    raise ValueError("绑定不存在，无法按修订号更新")
                binding_id = "bd_" + uuid.uuid4().hex[:16]
                conn.execute("INSERT INTO service_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (binding_id, agent_id, deployment_id, alias, service_id, contract,
                              encoded(tools), encoded(scope or {}), int(enabled), 1, now, now))
        return self.binding(binding_id)

    # -- incidents ------------------------------------------------------------
    def incident(self, incident_id: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM service_incidents WHERE id=?", (incident_id,)).fetchone()
        return _incident(row) if row else None

    def incidents(self, *, deployment_id: str | None = None, status: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM service_incidents WHERE 1=1", []
        if deployment_id:
            query += " AND deployment_id=?"
            args.append(deployment_id)
        if status:
            query += " AND status=?"
            args.append(status)
        with self.connection() as conn:
            rows = conn.execute(query + " ORDER BY opened_at DESC LIMIT 200", args).fetchall()
        return [_incident(row) for row in rows]

    def open_incident(self, deployment_id: str, generation: int | None, summary: str, affected_agents: list[str]) -> dict:
        """Merge by deployment+generation: one open incident per active generation."""
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT id,status FROM service_incidents WHERE deployment_id=? AND status IN ('open','monitoring')"
                               " ORDER BY opened_at DESC LIMIT 1", (deployment_id,)).fetchone()
            if row:
                # A new failure during monitoring reopens the same ticket.
                conn.execute("UPDATE service_incidents SET status='open',summary=? WHERE id=?",
                             (summary, row["id"]))
                incident_id = row["id"]
            else:
                incident_id = "inc_" + uuid.uuid4().hex[:16]
                now = time.time()
                conn.execute("INSERT INTO service_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (incident_id, deployment_id, generation, "open", summary, encoded(affected_agents),
                              "[]", now, now, None, None))
            row = conn.execute("SELECT affected_agents FROM service_incidents WHERE id=?", (incident_id,)).fetchone()
            agents = list(dict.fromkeys([*json.loads(row["affected_agents"]), *affected_agents]))
            conn.execute("UPDATE service_incidents SET affected_agents=?,updated_at=? WHERE id=?",
                         (encoded(agents), time.time(), incident_id))
        # Read back only after the write transaction commits; a nested read on a
        # second connection would not see the uncommitted insert.
        return self.incident(incident_id)

    def record_incident_action(self, incident_id: str, action: dict):
        with self.connection() as conn:
            row = conn.execute("SELECT actions FROM service_incidents WHERE id=? AND status IN ('open','monitoring')",
                               (incident_id,)).fetchone()
            if not row:
                raise ValueError("故障单不存在或已关闭")
            actions = json.loads(row["actions"])
            actions.append({**action, "at": time.time()})
            conn.execute("UPDATE service_incidents SET actions=?,updated_at=? WHERE id=?",
                         (encoded(actions), time.time(), incident_id))

    def update_incident(self, incident_id: str, *, status: str, summary: str | None = None,
                        close_evidence: str | None = None):
        if status not in INCIDENT_STATES:
            raise ValueError("故障单状态不正确")
        with self.connection() as conn:
            row = conn.execute("SELECT status FROM service_incidents WHERE id=?", (incident_id,)).fetchone()
            if not row:
                raise ValueError("故障单不存在")
            if row["status"] == "closed" and status != "closed":
                raise ValueError("已关闭的故障单不能重新打开")
            if status == "closed" and not close_evidence:
                raise ValueError("关闭故障单需要保留恢复验证证据")
            conn.execute("UPDATE service_incidents SET status=?,summary=coalesce(?,summary),close_evidence=?,updated_at=?,closed_at=?"
                         " WHERE id=?", (status, summary, close_evidence, time.time(),
                                         time.time() if status == "closed" else None, incident_id))

    # -- health events --------------------------------------------------------
    def health_event(self, deployment_id: str, level: str, state: str, detail: dict):
        with self.connection() as conn:
            conn.execute("INSERT INTO service_health_events(deployment_id,at,level,state,detail) VALUES(?,?,?,?,?)",
                         (deployment_id, time.time(), level, state, encoded(detail)))

    def health_events(self, deployment_id: str, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM service_health_events WHERE deployment_id=? ORDER BY seq DESC LIMIT ?",
                                (deployment_id, limit)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    # -- access grants --------------------------------------------------------
    def issue_grant(self, definition: dict, seconds: float) -> str:
        """Per-run service credentials; only the hash is persisted."""
        token = secrets.token_urlsafe(32)
        with self.connection(write=True) as conn:
            conn.execute("DELETE FROM service_grants WHERE expires<?", (time.time(),))
            conn.execute("INSERT INTO service_grants VALUES(?,?,?)", (digest(token), encoded(definition), time.time() + seconds))
        return token

    def verify_grant(self, token: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM service_grants WHERE hash=? AND expires>?",
                               (digest(token or ""), time.time())).fetchone()
        return json.loads(row["definition"]) if row else None

    def revoke_grants(self, *, deployment_id: str | None = None, agent_id: str | None = None):
        """Immediate revocation for safety: drop matching grants."""
        with self.connection(write=True) as conn:
            for row in conn.execute("SELECT hash,definition FROM service_grants").fetchall():
                definition = json.loads(row["definition"])
                if deployment_id and definition.get("deployment_id") != deployment_id:
                    continue
                if agent_id and definition.get("agent_id") != agent_id:
                    continue
                conn.execute("DELETE FROM service_grants WHERE hash=?", (row["hash"],))


def _binding(row) -> dict:
    item = dict(row)
    item["tools"] = json.loads(item["tools"])
    item["scope"] = json.loads(item["scope"])
    item["enabled"] = bool(item["enabled"])
    return item


def _incident(row) -> dict:
    item = dict(row)
    item["affected_agents"] = json.loads(item["affected_agents"])
    item["actions"] = json.loads(item["actions"])
    return item


def _valid_slug(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value))
