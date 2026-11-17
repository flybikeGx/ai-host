"""治理层业务门面：身份、授权、分权分域与审计的受信任实现。

web/cli 层在调用前先做权限判定（身份在网关/中间件校验），本层不持有「当前用户」，
只暴露可复用的受信任操作；所有写操作可携带 ``actor``/``trace_id`` 写审计。
"""
from __future__ import annotations

import os
import secrets
import time
import uuid
from pathlib import Path

from . import auth
from .rbac import check, effective_permissions
from .store import ControlStore


def _load_secret(root: Path) -> str:
    env = os.environ.get("AGENT_BASE_CONTROL_SECRET")
    if env:
        return env
    path = root / "control.secret"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    path.write_text(secret + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return secret


def _public_user(user: dict) -> dict:
    return {k: v for k, v in user.items() if k not in {"password_hash", "api_key_hash"}}


class Governance:
    def __init__(self, config):
        self.config = config
        self.store = ControlStore(config.data)
        self._secret = _load_secret(config.data)

    # ---- 引导 ----
    def bootstrap(self, admin_password: str | None = None) -> dict:
        password = admin_password or os.environ.get("AGENT_BASE_ADMIN_PASSWORD")
        if not password or self.store.get_user_by_username("admin") is not None:
            return {"admin_created": False}
        user = self.store.create_user("admin", password=auth.hash_password(password),
                                      display_name="平台管理员", user_type="person")
        role = self.store.get_role_by_name("platform_admin")
        self.store.grant_role(user["id"], role["id"], granted_by="system")
        self.store.audit(domain_id=None, user_id=user["id"], trace_id=None, level="gateway",
                         action="auth.bootstrap", detail={"username": "admin"})
        return {"admin_created": True}

    # ---- 认证 ----
    def login(self, username: str, password: str) -> dict:
        user = self.store.get_user_by_username(username)
        if user is None or not user["password_hash"]:
            raise ValueError("用户名或密码错误")
        if user["status"] == "disabled":
            raise ValueError("账号已停用")
        if user["status"] == "locked" and user["locked_until"] > time.time():
            raise ValueError("账号已锁定，请稍后再试")
        if not auth.verify_password(password, user["password_hash"]):
            failed = user["failed_count"] + 1
            fields = {"failed_count": failed}
            if failed >= 5:
                fields.update(status="locked", locked_until=time.time() + 300)
            self.store.update_user(user["id"], **fields)
            raise ValueError("用户名或密码错误")
        self.store.update_user(user["id"], failed_count=0, locked_until=0)
        return self._issue_session(user)

    def login_with_api_key(self, key: str) -> dict:
        user = self.store.get_user_by_api_key_hash(auth.hash_api_key(key))
        if user is None:
            raise ValueError("无效的 API key")
        if user["status"] != "active":
            raise ValueError("账号不可用")
        return self._issue_session(user)

    def _issue_session(self, user: dict) -> dict:
        payload = {
            "sub": user["id"], "username": user["username"], "user_type": user["user_type"],
            "home_domain_id": user["home_domain_id"], "jti": uuid.uuid4().hex,
        }
        ttl = 1200 if user["user_type"] == "person" else 86400 * 30
        token = auth.issue_token(payload, self._secret, ttl=ttl)
        return {"token": token, "expires_in": ttl, "user": _public_user(user)}

    def decode(self, token: str) -> dict:
        return auth.decode_token(token, self._secret)

    def principal(self, user_id: str) -> dict:
        user = _public_user(self.store.get_user(user_id))
        user["roles"] = self.store.list_user_roles(user_id)
        user["domains"] = self.store.list_memberships(user_id)
        return user

    # ---- 域 ----
    def create_domain(self, code: str, name: str, description: str = "", *,
                      actor: str | None = None, trace_id: str | None = None, **extra) -> dict:
        domain = self.store.create_domain(code, name, description, **extra)
        self.store.audit(domain_id=domain["id"], user_id=actor, trace_id=trace_id, level="gateway",
                         action="domain.create", detail={"code": code})
        return domain

    def list_domains(self) -> list[dict]:
        return self.store.list_domains()

    def get_domain(self, domain_id: str) -> dict:
        return self.store.get_domain(domain_id)

    def update_domain(self, domain_id: str, *, actor: str | None = None, trace_id: str | None = None, **fields) -> dict:
        domain = self.store.update_domain(domain_id, **fields)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="domain.update", detail=fields)
        return domain

    # ---- 用户 ----
    def create_user(self, username: str, *, password: str | None = None, actor: str | None = None,
                    trace_id: str | None = None, **fields) -> dict:
        user_type = fields.get("user_type", "person")
        if user_type == "service":
            api_key = secrets.token_urlsafe(32)
            user = self.store.create_user(username, api_key=auth.hash_api_key(api_key), user_type="service", **{
                k: v for k, v in fields.items() if k != "user_type"})
            self.store.audit(domain_id=fields.get("home_domain_id"), user_id=actor, trace_id=trace_id,
                             level="gateway", action="user.create", detail={"username": username, "user_type": "service"})
            return {**_public_user(user), "api_key": api_key}
        if not password:
            raise ValueError("person 账号必须提供初始密码")
        user = self.store.create_user(username, password=auth.hash_password(password), **{
            k: v for k, v in fields.items() if k != "user_type"})
        self.store.audit(domain_id=fields.get("home_domain_id"), user_id=actor, trace_id=trace_id,
                         level="gateway", action="user.create", detail={"username": username, "user_type": "person"})
        return _public_user(user)

    def list_users(self) -> list[dict]:
        return [_public_user(u) for u in self.store.list_users()]

    def get_user(self, user_id: str) -> dict:
        return _public_user(self.store.get_user(user_id))

    def update_user(self, user_id: str, *, actor: str | None = None, trace_id: str | None = None, **fields) -> dict:
        user = self.store.update_user(user_id, **fields)
        self.store.audit(domain_id=user["home_domain_id"], user_id=actor, trace_id=trace_id,
                         level="gateway", action="user.update", detail={"user_id": user_id, **fields})
        return _public_user(user)

    # ---- 角色 / 权限 ----
    def create_role(self, name: str, *, actor: str | None = None, trace_id: str | None = None, **fields) -> dict:
        role = self.store.create_role(name, **fields)
        self.store.audit(domain_id=None, user_id=actor, trace_id=trace_id, level="gateway",
                         action="role.create", detail={"name": name})
        return role

    def list_roles(self) -> list[dict]:
        return self.store.list_roles()

    def update_role(self, role_id: str, *, actor: str | None = None, trace_id: str | None = None, **fields) -> dict:
        role = self.store.update_role(role_id, **fields)
        self.store.audit(domain_id=None, user_id=actor, trace_id=trace_id, level="gateway",
                         action="role.update", detail={"role_id": role_id, **fields})
        return role

    def list_permissions(self) -> list[dict]:
        return self.store.list_permissions()

    # ---- 分组 ----
    def create_group(self, domain_id: str, kind: str, name: str, description: str = "",
                     *, actor: str | None = None, trace_id: str | None = None) -> dict:
        group = self.store.create_group(domain_id, kind, name, description)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="group.create", detail={"name": name, "kind": kind})
        return group

    def list_groups(self, domain_id: str | None = None) -> list[dict]:
        return self.store.list_groups(domain_id)

    # ---- 成员 / 授权 ----
    def add_membership(self, user_id: str, domain_id: str, *, actor: str | None = None,
                       trace_id: str | None = None) -> dict:
        result = self.store.add_membership(user_id, domain_id)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="membership.add", detail={"user_id": user_id, "domain_id": domain_id})
        return result

    def remove_membership(self, user_id: str, domain_id: str, *, actor: str | None = None,
                          trace_id: str | None = None):
        self.store.remove_membership(user_id, domain_id)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="membership.remove", detail={"user_id": user_id, "domain_id": domain_id})

    def grant_role(self, user_id: str, role_id: str, *, domain_id: str | None = None, group_id: str | None = None,
                   actor: str | None = None, trace_id: str | None = None) -> dict:
        grant = self.store.grant_role(user_id, role_id, domain_id=domain_id, group_id=group_id, granted_by=actor)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="role.grant", detail={"user_id": user_id, "role_id": role_id, "domain_id": domain_id})
        return grant

    def revoke_role(self, grant_id: str, *, actor: str | None = None, trace_id: str | None = None):
        self.store.revoke_role(grant_id)
        self.store.audit(domain_id=None, user_id=actor, trace_id=trace_id, level="gateway",
                         action="role.revoke", detail={"grant_id": grant_id})

    def grant_permission(self, user_id: str, permission: str, *, domain_id: str | None = None,
                         group_id: str | None = None, effect: str = "allow", actor: str | None = None,
                         trace_id: str | None = None) -> dict:
        grant = self.store.grant_permission(user_id, permission, domain_id=domain_id, group_id=group_id,
                                            effect=effect, granted_by=actor)
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action="permission.grant",
                         detail={"user_id": user_id, "permission": permission, "effect": effect})
        return grant

    def revoke_permission(self, grant_id: str, *, actor: str | None = None, trace_id: str | None = None):
        self.store.revoke_permission(grant_id)
        self.store.audit(domain_id=None, user_id=actor, trace_id=trace_id, level="gateway",
                         action="permission.revoke", detail={"grant_id": grant_id})

    def list_user_roles(self, user_id: str) -> list[dict]:
        return self.store.list_user_roles(user_id)

    def list_user_permissions(self, user_id: str) -> list[dict]:
        return self.store.list_user_permissions(user_id)

    # ---- RBAC ----
    def effective_permissions(self, user_id: str, domain_id: str | None, group_id: str | None = None) -> list[str]:
        return sorted(effective_permissions(self.store, user_id, domain_id, group_id))

    def authorize(self, user_id: str, permission: str, domain_id: str | None, group_id: str | None = None) -> bool:
        return check(self.store, user_id, permission, domain_id, group_id)

    # ---- 审计 ----
    def list_audit(self, **filters) -> list[dict]:
        return self.store.list_audit(**filters)
