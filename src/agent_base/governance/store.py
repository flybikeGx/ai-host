"""治理层数据模型：独立 ``control.sqlite``，保存域/用户/角色/权限/分组/审计。

与 ``store.py``（执行状态库）物理分离，二者只经 ``workspace_id/domain_id`` 契约关联，
不合并单库，符合「控制面与用户数据分离」的边界（见 GOVERNANCE_DESIGN.md §7.9）。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


BUILTIN_PERMISSIONS = [
    "system.manage", "domain.manage", "user.manage", "role.manage", "quota.manage",
    "service.read", "service.write", "service.manage",
    "tool.execute",
    "agent.run", "agent.read", "agent.cancel",
    "task.read", "task.write", "task.run",
    "resource.read", "resource.write", "resource.connect",
    "file.read", "file.write", "file.delete",
    "audit.read",
]

BUILTIN_ROLES = {
    "platform_admin": {
        "description": "平台管理员：系统配置 + 全部域/用户/角色管理",
        "permissions": set(BUILTIN_PERMISSIONS),
    },
    "auditor": {
        "description": "审计员：跨域只读（审计 + 资源）",
        "permissions": {"service.read", "agent.read", "task.read", "resource.read", "file.read", "audit.read"},
    },
    "domain_admin": {
        "description": "域管理员：本域用户/角色/服务/资源/配额管理",
        "permissions": {
            "user.manage", "role.manage", "service.manage", "quota.manage",
            "service.read", "service.write", "tool.execute",
            "agent.run", "agent.read", "agent.cancel",
            "task.read", "task.write", "task.run",
            "resource.read", "resource.write", "resource.connect",
            "file.read", "file.write", "file.delete", "audit.read",
        },
    },
    "developer": {
        "description": "开发者/普通成员：用知识库、服务、Agent、资源",
        "permissions": {
            "service.read", "tool.execute", "agent.run", "agent.read", "agent.cancel",
            "task.read", "task.write", "task.run",
            "resource.read", "resource.connect",
            "file.read", "file.write", "file.delete",
        },
    },
    "viewer": {
        "description": "只读成员：只查，不执行/写入",
        "permissions": {"service.read", "agent.read", "task.read", "resource.read", "file.read"},
    },
    "service_runner": {
        "description": "服务账号默认角色：仅执行被授权的任务/工具/连接",
        "permissions": {
            "service.read", "tool.execute", "agent.run", "agent.read",
            "task.read", "task.run", "resource.read", "resource.connect", "file.read",
        },
    },
}


_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
    max_agents INTEGER NOT NULL DEFAULT 2, storage_quota_mb INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE,
    password_hash TEXT, api_key_hash TEXT, display_name TEXT NOT NULL DEFAULT '',
    user_type TEXT NOT NULL DEFAULT 'person', status TEXT NOT NULL DEFAULT 'active',
    home_domain_id TEXT, mfa_enabled INTEGER NOT NULL DEFAULT 0,
    huawei_w3account TEXT,
    failed_count INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, is_builtin INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id TEXT NOT NULL, permission_id TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS user_domains (
    user_id TEXT NOT NULL, domain_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    joined_at REAL NOT NULL, PRIMARY KEY(user_id, domain_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, role_id TEXT NOT NULL,
    domain_id TEXT, group_id TEXT, granted_by TEXT, granted_at REAL NOT NULL,
    expires_at REAL, is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_permissions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, permission_id TEXT NOT NULL,
    domain_id TEXT, group_id TEXT, effect TEXT NOT NULL DEFAULT 'allow',
    granted_by TEXT, granted_at REAL NOT NULL, expires_at REAL, is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY, domain_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'service',
    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY, domain_id TEXT, user_id TEXT, trace_id TEXT,
    level TEXT NOT NULL DEFAULT 'gateway', action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY, domain_id TEXT NOT NULL, group_id TEXT,
    name TEXT NOT NULL, transport TEXT NOT NULL DEFAULT 'remote',
    endpoint TEXT NOT NULL DEFAULT '', credential_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active', description TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service_tools (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '', input_schema TEXT NOT NULL DEFAULT '{}',
    UNIQUE(service_id, name)
);
CREATE TABLE IF NOT EXISTS service_checks (
    id TEXT PRIMARY KEY, service_id TEXT NOT NULL,
    healthy INTEGER NOT NULL DEFAULT 0, latency REAL, checked_at REAL NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS user_roles_user ON user_roles(user_id, is_active);
CREATE INDEX IF NOT EXISTS user_permissions_user ON user_permissions(user_id, is_active);
CREATE INDEX IF NOT EXISTS audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS services_domain ON services(domain_id);
CREATE INDEX IF NOT EXISTS service_tools_service ON service_tools(service_id);
CREATE INDEX IF NOT EXISTS service_checks_service ON service_checks(service_id);
"""


def _row(conn, table: str, pk: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (pk,)).fetchone()
    if row is None:
        raise ValueError(f"不存在：{pk}")
    return dict(row)


class ControlStore:
    """治理面 SQLite 数据访问。所有方法返回纯 dict，不暴露连接。"""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = root / "control.sqlite"
        with self.connection() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
        self._seed_builtins()

    def _migrate(self, conn):
        """增量迁移：为既有 control.sqlite 补列，避免 CREATE IF NOT EXISTS 不生效。

        唯一约束用独立索引而非列内 UNIQUE——SQLite 的 ``ALTER TABLE ADD COLUMN``
        不允许带 UNIQUE，且与内联约束等价。
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "huawei_w3account" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN huawei_w3account TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_w3account ON users(huawei_w3account)")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- 初始化种子 ----
    def _seed_builtins(self):
        now = time.time()
        with self.connection() as conn:
            if conn.execute("SELECT count(*) FROM permissions").fetchone()[0] == 0:
                for name in BUILTIN_PERMISSIONS:
                    conn.execute("INSERT INTO permissions(id,name) VALUES(?,?)", (uuid.uuid4().hex, name))
            if conn.execute("SELECT count(*) FROM roles").fetchone()[0] == 0:
                for role_name, spec in BUILTIN_ROLES.items():
                    role_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO roles(id,name,is_builtin,description) VALUES(?,?,1,?)",
                                 (role_id, role_name, spec["description"]))
                    for perm in spec["permissions"]:
                        prow = conn.execute("SELECT id FROM permissions WHERE name=?", (perm,)).fetchone()
                        if prow:
                            conn.execute("INSERT OR IGNORE INTO role_permissions(role_id,permission_id) VALUES(?,?)",
                                         (role_id, prow[0]))
            if conn.execute("SELECT count(*) FROM domains").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO domains(id,code,name,description,status,max_agents,storage_quota_mb,created,updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "default", "默认域", "初始默认域", "active", 2, 0, now, now))

    # ---- 域 ----
    def create_domain(self, code: str, name: str, description: str = "", *, max_agents: int = 2,
                      storage_quota_mb: int = 0) -> dict:
        now = time.time()
        domain_id = uuid.uuid4().hex
        with self.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO domains(id,code,name,description,status,max_agents,storage_quota_mb,created,updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (domain_id, code, name, description, "active", max_agents, storage_quota_mb, now, now))
            except sqlite3.IntegrityError:
                raise ValueError(f"域码已存在：{code}")
        return self.get_domain(domain_id)

    def list_domains(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM domains ORDER BY created").fetchall()
        return [dict(r) for r in rows]

    def get_domain(self, domain_id: str) -> dict:
        with self.connection() as conn:
            return _row(conn, "domains", domain_id)

    def get_domain_by_code(self, code: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM domains WHERE code=?", (code,)).fetchone()
        if row is None:
            raise ValueError(f"域不存在：{code}")
        return dict(row)

    def update_domain(self, domain_id: str, **fields) -> dict:
        allowed = {"name", "description", "status", "max_agents", "storage_quota_mb"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效域字段")
        fields["updated"] = time.time()
        with self.connection() as conn:
            conn.execute(f"UPDATE domains SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                         (*fields.values(), domain_id))
            return _row(conn, "domains", domain_id)

    # ---- 用户 ----
    def create_user(self, username: str, *, password: str | None = None, api_key: str | None = None,
                    email: str | None = None, display_name: str = "", user_type: str = "person",
                    home_domain_id: str | None = None, huawei_w3account: str | None = None) -> dict:
        if user_type not in {"person", "service"}:
            raise ValueError("user_type 只支持 person / service")
        now = time.time()
        user_id = uuid.uuid4().hex
        with self.connection() as conn:
            try:
                conn.execute(
                    "INSERT INTO users(id,username,email,password_hash,api_key_hash,display_name,user_type,status,"
                    "home_domain_id,mfa_enabled,huawei_w3account,failed_count,locked_until,created,updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0,?,0,0,?,?)",
                    (user_id, username, email, password, api_key, display_name, user_type, "active",
                     home_domain_id, huawei_w3account, now, now))
            except sqlite3.IntegrityError:
                raise ValueError(f"用户名、邮箱或工号已存在：{username}")
        return self.get_user(user_id)

    def list_users(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created").fetchall()
        return [dict(r) for r in rows]

    def get_user(self, user_id: str) -> dict:
        with self.connection() as conn:
            return _row(conn, "users", user_id)

    def get_user_by_username(self, username: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def get_user_by_api_key_hash(self, key_hash: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE api_key_hash=?", (key_hash,)).fetchone()
        return dict(row) if row else None

    def get_user_by_w3account(self, w3account: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE huawei_w3account=?", (w3account,)).fetchone()
        return dict(row) if row else None

    def update_user(self, user_id: str, **fields) -> dict:
        allowed = {"display_name", "status", "email", "home_domain_id", "password_hash",
                   "huawei_w3account", "failed_count", "locked_until"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效用户字段")
        fields["updated"] = time.time()
        with self.connection() as conn:
            conn.execute(f"UPDATE users SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                         (*fields.values(), user_id))
            return _row(conn, "users", user_id)

    # ---- 角色 / 权限 ----
    def list_roles(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM roles ORDER BY is_builtin DESC, name").fetchall()
            roles = []
            for r in rows:
                item = dict(r)
                perms = conn.execute(
                    "SELECT p.name FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id"
                    " WHERE rp.role_id=?", (r["id"],)).fetchall()
                item["permissions"] = [p[0] for p in perms]
                roles.append(item)
        return roles

    def get_role(self, role_id: str) -> dict:
        with self.connection() as conn:
            role = _row(conn, "roles", role_id)
            perms = conn.execute(
                "SELECT p.name FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id"
                " WHERE rp.role_id=?", (role_id,)).fetchall()
            role["permissions"] = [p[0] for p in perms]
            return role

    def get_role_by_name(self, name: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM roles WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"角色不存在：{name}")
        return self.get_role(row["id"])

    def create_role(self, name: str, description: str = "", permissions: list[str] | None = None) -> dict:
        unknown = set(permissions or []) - set(BUILTIN_PERMISSIONS)
        if unknown:
            raise ValueError(f"未知权限：{', '.join(sorted(unknown))}")
        role_id = uuid.uuid4().hex
        with self.connection() as conn:
            try:
                conn.execute("INSERT INTO roles(id,name,is_builtin,description) VALUES(?,?,0,?)",
                             (role_id, name, description))
            except sqlite3.IntegrityError:
                raise ValueError(f"角色已存在：{name}")
            for perm in permissions or []:
                prow = conn.execute("SELECT id FROM permissions WHERE name=?", (perm,)).fetchone()
                conn.execute("INSERT INTO role_permissions(role_id,permission_id) VALUES(?,?)", (role_id, prow[0]))
        return self.get_role(role_id)

    def update_role(self, role_id: str, *, description: str | None = None, permissions: list[str] | None = None) -> dict:
        role = self.get_role(role_id)
        if role["is_builtin"] and permissions is not None:
            raise ValueError("内置角色不可修改权限")
        with self.connection() as conn:
            if description is not None:
                conn.execute("UPDATE roles SET description=? WHERE id=?", (description, role_id))
            if permissions is not None:
                conn.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
                for perm in permissions:
                    prow = conn.execute("SELECT id FROM permissions WHERE name=?", (perm,)).fetchone()
                    if prow is None:
                        raise ValueError(f"未知权限：{perm}")
                    conn.execute("INSERT INTO role_permissions(role_id,permission_id) VALUES(?,?)", (role_id, prow[0]))
        return self.get_role(role_id)

    def list_permissions(self) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM permissions ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ---- 分组 ----
    def create_group(self, domain_id: str, kind: str, name: str, description: str = "") -> dict:
        if kind not in {"service", "agent"}:
            raise ValueError("kind 只支持 service / agent")
        self.get_domain(domain_id)
        group_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute("INSERT INTO groups(id,domain_id,kind,name,description) VALUES(?,?,?,?,?)",
                         (group_id, domain_id, kind, name, description))
        return self.get_group(group_id)

    def get_group(self, group_id: str) -> dict:
        with self.connection() as conn:
            return _row(conn, "groups", group_id)

    def list_groups(self, domain_id: str | None = None) -> list[dict]:
        with self.connection() as conn:
            if domain_id:
                rows = conn.execute("SELECT * FROM groups WHERE domain_id=? ORDER BY name", (domain_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ---- 成员与授权 ----
    def add_membership(self, user_id: str, domain_id: str) -> dict:
        self.get_user(user_id)
        self.get_domain(domain_id)
        joined_at = time.time()
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO user_domains(user_id,domain_id,status,joined_at) VALUES(?,?,?,?)",
                         (user_id, domain_id, "active", joined_at))
        return {"user_id": user_id, "domain_id": domain_id, "status": "active", "joined_at": joined_at}

    def remove_membership(self, user_id: str, domain_id: str):
        with self.connection() as conn:
            conn.execute("DELETE FROM user_domains WHERE user_id=? AND domain_id=?", (user_id, domain_id))

    def list_memberships(self, user_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM user_domains WHERE user_id=? AND status='active'", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def grant_role(self, user_id: str, role_id: str, *, domain_id: str | None = None, group_id: str | None = None,
                   granted_by: str | None = None, expires_at: float | None = None) -> dict:
        self.get_user(user_id)
        self.get_role(role_id)
        grant_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO user_roles(id,user_id,role_id,domain_id,group_id,granted_by,granted_at,expires_at,is_active)"
                " VALUES(?,?,?,?,?,?,?,?,1)",
                (grant_id, user_id, role_id, domain_id, group_id, granted_by, time.time(), expires_at))
        with self.connection() as conn:
            return _row(conn, "user_roles", grant_id)

    def revoke_role(self, grant_id: str):
        with self.connection() as conn:
            conn.execute("UPDATE user_roles SET is_active=0 WHERE id=?", (grant_id,))

    def list_user_roles(self, user_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT ur.*, r.name AS role_name FROM user_roles ur JOIN roles r ON r.id=ur.role_id"
                " WHERE ur.user_id=? AND ur.is_active=1 ORDER BY ur.granted_at", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def grant_permission(self, user_id: str, permission: str, *, domain_id: str | None = None,
                         group_id: str | None = None, effect: str = "allow", granted_by: str | None = None,
                         expires_at: float | None = None) -> dict:
        if effect not in {"allow", "deny"}:
            raise ValueError("effect 只支持 allow / deny")
        with self.connection() as conn:
            prow = conn.execute("SELECT id FROM permissions WHERE name=?", (permission,)).fetchone()
            if prow is None:
                raise ValueError(f"未知权限：{permission}")
            grant_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO user_permissions(id,user_id,permission_id,domain_id,group_id,effect,granted_by,granted_at,expires_at,is_active)"
                " VALUES(?,?,?,?,?,?,?,?,?,1)",
                (grant_id, user_id, prow[0], domain_id, group_id, effect, granted_by, time.time(), expires_at))
            return _row(conn, "user_permissions", grant_id)

    def revoke_permission(self, grant_id: str):
        with self.connection() as conn:
            conn.execute("UPDATE user_permissions SET is_active=0 WHERE id=?", (grant_id,))

    def list_user_permissions(self, user_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT up.*, p.name AS permission FROM user_permissions up JOIN permissions p ON p.id=up.permission_id"
                " WHERE up.user_id=? AND up.is_active=1 ORDER BY up.granted_at", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- RBAC 求值 ----
    def role_permission_names(self, user_id: str, domain_id: str | None, group_id: str | None) -> set[str]:
        now = time.time()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.name FROM user_roles ur"
                " JOIN roles r ON r.id=ur.role_id"
                " JOIN role_permissions rp ON rp.role_id=r.id"
                " JOIN permissions p ON p.id=rp.permission_id"
                " WHERE ur.user_id=? AND ur.is_active=1 AND (ur.expires_at IS NULL OR ur.expires_at>?)"
                " AND (ur.domain_id IS NULL OR ur.domain_id=?)"
                " AND (ur.group_id IS NULL OR ur.group_id=?)",
                (user_id, now, domain_id, group_id)).fetchall()
        return {r[0] for r in rows}

    def override_permission_names(self, user_id: str, domain_id: str | None, group_id: str | None) -> dict:
        now = time.time()
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT p.name, up.effect FROM user_permissions up JOIN permissions p ON p.id=up.permission_id"
                " WHERE up.user_id=? AND up.is_active=1 AND (up.expires_at IS NULL OR up.expires_at>?)"
                " AND (up.domain_id IS NULL OR up.domain_id=?)"
                " AND (up.group_id IS NULL OR up.group_id=?)",
                (user_id, now, domain_id, group_id)).fetchall()
        result = {"allow": set(), "deny": set()}
        for name, effect in rows:
            result[effect].add(name)
        return result

    # ---- 服务注册 ----
    def create_service(self, domain_id: str, name: str, transport: str = "remote", endpoint: str = "",
                       *, group_id: str | None = None, credential_ref: str | None = None,
                       description: str = "") -> dict:
        if transport not in {"remote", "embedded"}:
            raise ValueError("transport 只支持 remote / embedded")
        self.get_domain(domain_id)
        if group_id is not None:
            group = self.get_group(group_id)
            if group["domain_id"] != domain_id:
                raise ValueError("分组不属于该域")
        now = time.time()
        service_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO services(id,domain_id,group_id,name,transport,endpoint,credential_ref,status,"
                "description,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (service_id, domain_id, group_id, name, transport, endpoint, credential_ref, "active",
                 description, now, now))
        return self.get_service(service_id)

    def list_services(self, domain_id: str | None = None) -> list[dict]:
        with self.connection() as conn:
            if domain_id:
                rows = conn.execute("SELECT * FROM services WHERE domain_id=? ORDER BY created", (domain_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM services ORDER BY created").fetchall()
        return [dict(r) for r in rows]

    def get_service(self, service_id: str) -> dict:
        with self.connection() as conn:
            return _row(conn, "services", service_id)

    def get_service_by_name(self, domain_id: str, name: str) -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM services WHERE domain_id=? AND name=?", (domain_id, name)).fetchone()
        return dict(row) if row else None

    def update_service(self, service_id: str, **fields) -> dict:
        allowed = {"name", "transport", "endpoint", "credential_ref", "group_id", "status", "description"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效服务字段")
        if "transport" in fields and fields["transport"] not in {"remote", "embedded"}:
            raise ValueError("transport 只支持 remote / embedded")
        if "group_id" in fields and fields["group_id"] is not None:
            group = self.get_group(fields["group_id"])
            if group["domain_id"] != self.get_service(service_id)["domain_id"]:
                raise ValueError("分组不属于该域")
        fields["updated"] = time.time()
        with self.connection() as conn:
            conn.execute(f"UPDATE services SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                         (*fields.values(), service_id))
            return _row(conn, "services", service_id)

    def delete_service(self, service_id: str):
        with self.connection() as conn:
            conn.execute("DELETE FROM service_tools WHERE service_id=?", (service_id,))
            conn.execute("DELETE FROM service_checks WHERE service_id=?", (service_id,))
            conn.execute("DELETE FROM services WHERE id=?", (service_id,))

    def set_tools(self, service_id: str, tools: list[dict]):
        self.get_service(service_id)
        with self.connection() as conn:
            conn.execute("DELETE FROM service_tools WHERE service_id=?", (service_id,))
            for tool in tools:
                conn.execute(
                    "INSERT INTO service_tools(id,service_id,name,description,input_schema) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, service_id, tool["name"], tool.get("description", ""),
                     json.dumps(tool.get("input_schema") or {}, ensure_ascii=False)))

    def list_tools(self, service_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM service_tools WHERE service_id=? ORDER BY name", (service_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["input_schema"] = json.loads(item["input_schema"]) if item["input_schema"] else {}
            out.append(item)
        return out

    def record_check(self, service_id: str, healthy: bool, latency: float | None, error: str = "") -> dict:
        check_id = uuid.uuid4().hex
        checked_at = time.time()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO service_checks(id,service_id,healthy,latency,checked_at,error) VALUES(?,?,?,?,?,?)",
                (check_id, service_id, 1 if healthy else 0, latency, checked_at, error))
        return {"id": check_id, "service_id": service_id, "healthy": healthy, "latency": latency,
                "checked_at": checked_at, "error": error}

    def list_checks(self, service_id: str, limit: int = 50) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM service_checks WHERE service_id=? ORDER BY checked_at DESC LIMIT ?",
                (service_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---- 审计 ----
    def audit(self, *, domain_id: str | None, user_id: str | None, trace_id: str | None, level: str,
              action: str, detail: dict | None = None):
        with self.connection() as conn:
            conn.execute("INSERT INTO audit_log(id,domain_id,user_id,trace_id,level,action,detail,at)"
                         " VALUES(?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, domain_id, user_id, trace_id, level, action,
                          json.dumps(detail or {}, ensure_ascii=False), time.time()))

    def list_audit(self, *, domain_id: str | None = None, user_id: str | None = None,
                   trace_id: str | None = None, limit: int = 100) -> list[dict]:
        clause, args = [], []
        for key, value in (("domain_id", domain_id), ("user_id", user_id), ("trace_id", trace_id)):
            if value is not None:
                clause.append(f"{key}=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clause)) if clause else ""
        args.append(limit)
        with self.connection() as conn:
            rows = conn.execute(f"SELECT * FROM audit_log{where} ORDER BY at DESC LIMIT ?", args).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"]) if item["detail"] else {}
            out.append(item)
        return out
