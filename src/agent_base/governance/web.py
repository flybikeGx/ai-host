"""治理面 HTTP API：``/api/v1`` 前缀，JWT Bearer 鉴权 + 权限校验。

独立于既有单用户 ``/api/*``（后者仍用 AGENT_BASE_WEB_TOKEN 的 Bearer token）。
本模块只挂路由，不新建 FastAPI 实例；鉴权为签名的 ``control.secret`` JWT，权限以
``Governance.authorize`` 数据库求值为准（GOVERNANCE_DESIGN.md §4.1）。
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .service import Governance


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class ApiKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str


class DomainInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    name: str
    description: str = ""
    max_agents: int = Field(default=2, ge=1, le=64)
    storage_quota_mb: int = Field(default=0, ge=0)


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str | None = None
    display_name: str = ""
    email: str | None = None
    user_type: str = "person"
    home_domain_id: str | None = None


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_id: str
    kind: str = "service"
    name: str
    description: str = ""


class MembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    domain_id: str


class RoleGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    role_id: str
    domain_id: str | None = None
    group_id: str | None = None


class PermissionGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    permission: str
    effect: str = "allow"
    domain_id: str | None = None
    group_id: str | None = None


def register(app: FastAPI, config):
    gov = Governance(config)
    app.state.governance = gov
    gov.bootstrap()

    def current_user(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "需要 Bearer token")
        try:
            return gov.decode(authorization[7:])
        except ValueError as error:
            raise HTTPException(401, str(error))

    def permission(perm: str):
        def dep(request: Request, payload: dict = Depends(current_user)) -> dict:
            domain_id = request.headers.get("x-domain-id")
            if not gov.authorize(payload["sub"], perm, domain_id):
                raise HTTPException(403, f"缺少权限：{perm}")
            return payload
        return dep

    def actor(payload: dict = Depends(current_user)) -> str:
        return payload["sub"]

    @app.post("/api/v1/auth/login")
    def login(body: LoginInput):
        try:
            return gov.login(body.username, body.password)
        except ValueError as error:
            raise HTTPException(401, str(error))

    @app.post("/api/v1/auth/api-key")
    def login_api_key(body: ApiKeyInput):
        try:
            return gov.login_with_api_key(body.key)
        except ValueError as error:
            raise HTTPException(401, str(error))

    @app.get("/api/v1/me")
    def me(payload: dict = Depends(current_user)):
        try:
            user = gov.principal(payload["sub"])
        except ValueError as error:
            raise HTTPException(404, str(error))
        user["effective_permissions"] = gov.effective_permissions(payload["sub"], None)
        return user

    @app.get("/api/v1/permissions")
    def list_permissions(_: dict = Depends(current_user)):
        return gov.list_permissions()

    @app.get("/api/v1/domains")
    def list_domains(_: dict = Depends(current_user)):
        return gov.list_domains()

    @app.post("/api/v1/domains", status_code=201)
    def create_domain(body: DomainInput, payload: dict = Depends(permission("domain.manage")), who: str = Depends(actor)):
        return gov.create_domain(body.code, body.name, body.description,
                                 max_agents=body.max_agents, storage_quota_mb=body.storage_quota_mb,
                                 actor=who, trace_id=payload.get("jti"))

    @app.patch("/api/v1/domains/{domain_id}")
    def update_domain(domain_id: str, body: dict, payload: dict = Depends(permission("domain.manage")),
                      who: str = Depends(actor)):
        allowed = {"name", "description", "status", "max_agents", "storage_quota_mb"}
        return gov.update_domain(domain_id, actor=who, trace_id=payload.get("jti"),
                                 **{k: body[k] for k in allowed if k in body})

    @app.get("/api/v1/users")
    def list_users(_: dict = Depends(permission("user.manage"))):
        return gov.list_users()

    @app.post("/api/v1/users", status_code=201)
    def create_user(body: UserInput, payload: dict = Depends(permission("user.manage")), who: str = Depends(actor)):
        return gov.create_user(body.username, password=body.password, display_name=body.display_name,
                               email=body.email, user_type=body.user_type, home_domain_id=body.home_domain_id,
                               actor=who, trace_id=payload.get("jti"))

    @app.patch("/api/v1/users/{user_id}")
    def update_user(user_id: str, body: dict, payload: dict = Depends(permission("user.manage")),
                    who: str = Depends(actor)):
        allowed = {"display_name", "status", "email", "home_domain_id"}
        return gov.update_user(user_id, actor=who, trace_id=payload.get("jti"),
                               **{k: body[k] for k in allowed if k in body})

    @app.get("/api/v1/roles")
    def list_roles(_: dict = Depends(current_user)):
        return gov.list_roles()

    @app.post("/api/v1/roles", status_code=201)
    def create_role(body: RoleInput, payload: dict = Depends(permission("role.manage")), who: str = Depends(actor)):
        return gov.create_role(body.name, description=body.description, permissions=body.permissions,
                               actor=who, trace_id=payload.get("jti"))

    @app.get("/api/v1/groups")
    def list_groups(domain_id: str | None = None, _: dict = Depends(current_user)):
        return gov.list_groups(domain_id)

    @app.post("/api/v1/groups", status_code=201)
    def create_group(body: GroupInput, payload: dict = Depends(permission("service.manage")), who: str = Depends(actor)):
        return gov.create_group(body.domain_id, body.kind, body.name, body.description,
                                actor=who, trace_id=payload.get("jti"))

    @app.post("/api/v1/memberships", status_code=201)
    def add_membership(body: MembershipInput, payload: dict = Depends(permission("user.manage")),
                       who: str = Depends(actor)):
        return gov.add_membership(body.user_id, body.domain_id, actor=who, trace_id=payload.get("jti"))

    @app.delete("/api/v1/memberships")
    def remove_membership(body: MembershipInput, payload: dict = Depends(permission("user.manage")),
                          who: str = Depends(actor)):
        gov.remove_membership(body.user_id, body.domain_id, actor=who, trace_id=payload.get("jti"))
        return {"ok": True}

    @app.post("/api/v1/grants/roles", status_code=201)
    def grant_role(body: RoleGrantInput, payload: dict = Depends(permission("role.manage")), who: str = Depends(actor)):
        return gov.grant_role(body.user_id, body.role_id, domain_id=body.domain_id, group_id=body.group_id,
                              actor=who, trace_id=payload.get("jti"))

    @app.delete("/api/v1/grants/roles/{grant_id}")
    def revoke_role(grant_id: str, payload: dict = Depends(permission("role.manage")), who: str = Depends(actor)):
        gov.revoke_role(grant_id, actor=who, trace_id=payload.get("jti"))
        return {"ok": True}

    @app.post("/api/v1/grants/permissions", status_code=201)
    def grant_permission(body: PermissionGrantInput, payload: dict = Depends(permission("role.manage")),
                         who: str = Depends(actor)):
        return gov.grant_permission(body.user_id, body.permission, domain_id=body.domain_id, group_id=body.group_id,
                                    effect=body.effect, actor=who, trace_id=payload.get("jti"))

    @app.delete("/api/v1/grants/permissions/{grant_id}")
    def revoke_permission(grant_id: str, payload: dict = Depends(permission("role.manage")), who: str = Depends(actor)):
        gov.revoke_permission(grant_id, actor=who, trace_id=payload.get("jti"))
        return {"ok": True}

    @app.get("/api/v1/audit")
    def list_audit(domain_id: str | None = None, user_id: str | None = None, trace_id: str | None = None,
                   limit: int = 100, _: dict = Depends(permission("audit.read"))):
        return gov.list_audit(domain_id=domain_id, user_id=user_id, trace_id=trace_id, limit=limit)
