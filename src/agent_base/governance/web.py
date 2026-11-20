"""治理面 HTTP API：``/api/v1``，签名 Bearer 或同源浏览器会话 + 权限校验。

独立于既有单用户 ``/api/*``（后者仍用 AGENT_BASE_WEB_TOKEN 的 Bearer token）。
本模块只挂路由，不新建 FastAPI 实例；鉴权为签名的 ``control.secret`` JWT，权限以
``Governance.authorize`` 数据库求值为准（GOVERNANCE_DESIGN.md §4.1）。
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..identity import Identity, IdentityError, get_identity, get_bearer_identity
from .gateway import Gateway
from .registry import ServiceRegistry
from .service import Governance


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class ApiKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str


class OssLoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


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


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_id: str
    name: str
    transport: str = "remote"
    endpoint: str | dict | None = None
    group_id: str | None = None
    credential_ref: str | None = None
    description: str = ""


class ServiceToolsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[dict] = Field(default_factory=list)


class GatewayCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_id: str
    tool: str
    arguments: dict = Field(default_factory=dict)


def register(app: FastAPI, config):
    gov = Governance(config)
    registry = ServiceRegistry(gov.store)
    gateway = Gateway(gov, registry)
    app.state.governance = gov
    app.state.registry = registry
    app.state.gateway = gateway
    gov.bootstrap()

    def current_user(request: Request, identity: Identity = Depends(get_identity)) -> dict:
        # Adapter for existing governance audit/actor contracts. No credential
        # claims are trusted here; identity has already re-read the user.
        return {"sub": identity.user_id, "jti": request.state.identity_trace_id}

    def permission(perm: str):
        def dep(identity: Identity = Depends(get_identity),
                payload: dict = Depends(current_user)) -> dict:
            if not identity.can(perm):
                raise IdentityError(403, "permission_denied", f"缺少权限：{perm}")
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

    @app.post("/api/v1/auth/oss")
    def login_oss(body: OssLoginInput):
        try:
            return gov.login_with_oss(body.token)
        except ValueError as error:
            raise HTTPException(401, str(error))

    @app.post("/api/v1/auth/refresh")
    def refresh(identity: Identity = Depends(get_bearer_identity)):
        return gov._issue_session(gov.store.get_user(identity.user_id), ttl=1200)

    @app.get("/api/v1/me")
    def me(identity: Identity = Depends(get_identity)):
        user = gov.principal(identity.user_id) if identity.user_id else {
            "id": None, "username": None, "user_type": "bootstrap", "roles": []}
        memberships = {m["domain_id"]: m for m in user.get("domains", [])}
        user["platform_admin"] = identity.platform_admin
        user["home_domain_id"] = identity.home_domain_id
        user["domains"] = [
            {**d, "joined_at": 0.0, **memberships.get(d["id"], {}),
             "permissions": sorted(identity.permissions if identity.is_bootstrap else
                                   gov.effective_permissions(identity.user_id, d["id"]))}
            for d in gov.list_domains() if d["id"] in identity.domains]
        user["effective_permissions"] = sorted(identity.permissions) if identity.is_bootstrap else gov.effective_permissions(identity.user_id, None)
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

    # ---- 服务注册中心 ----
    @app.get("/api/v1/registry/services")
    def registry_list(domain_id: str | None = None, request: Request = None,
                      _: dict = Depends(permission("service.read"))):
        scope = domain_id or (request.headers.get("x-domain-id") if request else None)
        return registry.list(scope)

    @app.post("/api/v1/registry/services", status_code=201)
    def registry_register(body: ServiceInput, payload: dict = Depends(permission("service.write")),
                          who: str = Depends(actor)):
        return registry.register(body.domain_id, body.name, body.transport, body.endpoint or "",
                                 group_id=body.group_id, credential_ref=body.credential_ref,
                                 description=body.description, actor=who, trace_id=payload.get("jti"))

    @app.get("/api/v1/registry/services/{service_id}")
    def registry_get(service_id: str, _: dict = Depends(permission("service.read"))):
        return registry.get(service_id)

    @app.patch("/api/v1/registry/services/{service_id}")
    def registry_update(service_id: str, body: dict, payload: dict = Depends(permission("service.write")),
                        who: str = Depends(actor)):
        allowed = {"name", "transport", "endpoint", "credential_ref", "group_id", "status", "description"}
        return registry.update(service_id, actor=who, trace_id=payload.get("jti"),
                               **{k: body[k] for k in allowed if k in body})

    @app.delete("/api/v1/registry/services/{service_id}")
    def registry_remove(service_id: str, payload: dict = Depends(permission("service.manage")),
                        who: str = Depends(actor)):
        registry.remove(service_id, actor=who, trace_id=payload.get("jti"))
        return {"ok": True}

    @app.put("/api/v1/registry/services/{service_id}/tools")
    def registry_tools(service_id: str, body: ServiceToolsInput,
                       payload: dict = Depends(permission("service.write")), who: str = Depends(actor)):
        return registry.set_tools(service_id, body.tools, actor=who, trace_id=payload.get("jti"))

    @app.post("/api/v1/registry/services/{service_id}/check")
    def registry_check(service_id: str, payload: dict = Depends(permission("service.read")),
                       who: str = Depends(actor)):
        return registry.check(service_id, actor=who, trace_id=payload.get("jti"))

    @app.get("/api/v1/registry/services/{service_id}/server-json")
    def registry_server_json(service_id: str, _: dict = Depends(permission("service.read"))):
        return registry.server_json(service_id)

    # ---- 网关 ----
    @app.post("/api/v1/gateway/call")
    def gateway_call(body: GatewayCallInput, payload: dict = Depends(current_user), request: Request = None):
        try:
            return gateway.call(service_id=body.service_id, tool=body.tool, arguments=body.arguments,
                                user_id=payload["sub"],
                                domain_id=request.headers.get("x-domain-id") if request else None,
                                trace_id=payload.get("jti"))
        except PermissionError as error:
            raise HTTPException(403, str(error))
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.api_route("/api/v1/gateway/services/{service_id}/mcp", methods=["GET", "POST", "DELETE"])
    async def gateway_relay(service_id: str, request: Request, payload: dict = Depends(current_user)):
        try:
            return await gateway.relay(service_id=service_id, user_id=payload["sub"], request=request,
                                       trace_id=payload.get("jti"))
        except PermissionError as error:
            raise HTTPException(403, str(error))
        except ValueError as error:
            raise HTTPException(404, str(error))
