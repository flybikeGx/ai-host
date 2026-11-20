"""Request-edge identity for governance and future chat route dependencies.

No credential material is retained on Identity. SYSTEM is for explicit internal
calls only; HTTP resolution never constructs a system identity.
"""
from __future__ import annotations

import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass, replace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .governance.store import BUILTIN_PERMISSIONS
from .web_auth import COOKIE


class IdentityError(HTTPException):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, detail)
        self.code = code


@dataclass(frozen=True)
class Identity:
    user_id: str | None
    username: str | None
    user_type: str
    platform_admin: bool
    home_domain_id: str | None
    domains: frozenset[str]
    current_domain: str | None
    permissions: frozenset[str]
    mode: str

    @property
    def is_bootstrap(self) -> bool:
        return self.mode in {"token", "bootstrap"}

    def can(self, permission: str) -> bool:
        return self.mode == "system" or permission in self.permissions


SYSTEM = Identity(None, None, "system", True, None, frozenset(), None,
                  frozenset(BUILTIN_PERMISSIONS), "system")


def system_identity(gov) -> Identity:
    """Bind the internal constant to the current domain catalog, never to HTTP input."""
    return replace(SYSTEM, domains=frozenset(d["id"] for d in gov.list_domains()))


def is_platform_admin(gov, user_id: str) -> bool:
    return any(r["role_name"] == "platform_admin" and r["domain_id"] is None
               and (r["expires_at"] is None or r["expires_at"] > time.time())
               for r in gov.store.list_user_roles(user_id))


def resolve_domain(header: str | None, *, page: bool, bootstrap: bool,
                   domains: frozenset[str], home_domain_id: str | None,
                   enabled_domains: list[dict]) -> str | None:
    enabled = {d["id"] for d in enabled_domains}
    if not page:
        if header is not None and header not in enabled:
            raise IdentityError(403, "domain_forbidden", "域不存在或已停用")
        return header  # Control plane retains global grants without membership.
    if bootstrap:
        if header in enabled:
            return header
        ordered = sorted(enabled_domains, key=lambda d: (d["code"] != "default", d["created"], d["id"]))
        if ordered:
            return ordered[0]["id"]
    elif header is not None:
        if header in domains:
            return header
    elif home_domain_id in domains:
        return home_domain_id
    elif len(domains) == 1:
        return next(iter(domains))
    elif len(domains) > 1:
        raise IdentityError(400, "domain_required", "请选择当前域")
    raise IdentityError(403, "domain_forbidden", "域不可用或未加入")


class IdentityResolver:
    def __init__(self, app, config):
        self.app = app
        self.gov = app.state.governance
        self.mode = config.web_auth_mode()
        import os
        self.shared_token = os.environ.get("AGENT_BASE_WEB_TOKEN")
        if self.mode == "hybrid" and not self.shared_token and self.gov.store.list_users():
            logging.getLogger(__name__).warning("本机引导模式，未启用登录")

    def resolve(self, request: Request, *, page: bool = False, bearer_only: bool = False) -> Identity:
        gov = self.gov
        authorization = request.headers.get("authorization")
        token = None
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not token or token.strip() != token:
                raise IdentityError(401, "token_invalid", "令牌格式无效")
        bootstrap = False
        mode = "jwt"
        user = None
        claims = None
        if token and self.shared_token and secrets.compare_digest(token.encode(), self.shared_token.encode()):
            if self.mode != "hybrid" or bearer_only:
                raise IdentityError(401, "token_invalid", "不接受共享令牌")
            bootstrap, mode = True, "token"
        elif token:
            try:
                claims = gov.decode(token)
            except ValueError as error:
                code = "token_expired" if str(error) == "令牌已过期" else "token_invalid"
                raise IdentityError(401, code, str(error))
        elif not bearer_only and request.cookies.get(COOKIE):
            claims = self.app.state.browser_auth.identity(request)
            if claims is None:
                with gov.store.connection() as conn:
                    row = conn.execute(
                        "SELECT u.status,u.locked_until FROM browser_sessions s JOIN users u ON u.id=s.user_id"
                        " WHERE s.hash=? AND s.expires>?",
                        (self.app.state.browser_auth.digest(request.cookies[COOKIE]), time.time())).fetchone()
                if row and (row["status"] != "active" or row["locked_until"] > time.time()):
                    raise IdentityError(401, "user_disabled", "账号不可用")
                raise IdentityError(401, "token_invalid", "登录已失效")
        elif not bearer_only and self.mode == "hybrid" and not self.shared_token and self._loopback(request):
            bootstrap, mode = True, "bootstrap"
        else:
            raise IdentityError(401, "token_missing", "需要 Bearer token 或登录会话")
        if bootstrap:
            user = next((u for u in sorted(gov.store.list_users(), key=lambda u: (u["created"], u["id"]))
                         if u["status"] == "active" and is_platform_admin(gov, u["id"])), None)
        else:
            try:
                user = gov.store.get_user(claims["sub"])
            except (ValueError, KeyError):
                raise IdentityError(401, "user_disabled", "账号不可用")
            if user["status"] != "active" or user["locked_until"] > time.time():
                raise IdentityError(401, "user_disabled", "账号不可用")
        return self._finish(request, page=page, bootstrap=bootstrap, mode=mode, user=user, claims=claims)

    def resolve_page(self, request: Request) -> Identity:
        """Page identity, preserving the account-free local workspace (§3.3).

        Once accounts exist, the normal hybrid rules apply (loopback BOOTSTRAP
        or a real session); before any account is created the local workspace
        stays anonymous-bootstrap exactly as the pre-governance page did.
        """
        try:
            return self.resolve(request, page=True)
        except IdentityError as error:
            if (error.code == "token_missing" and self.mode == "hybrid" and not self.shared_token
                    and not self.app.state.browser_auth.required()):
                return self._finish(request, page=True, bootstrap=True, mode="bootstrap",
                                    user=None, claims=None)
            raise

    def _finish(self, request, *, page, bootstrap, mode, user, claims) -> Identity:
        gov = self.gov
        admin = bootstrap or is_platform_admin(gov, user["id"])
        enabled = [d for d in gov.list_domains() if d["status"] == "active"]
        memberships = set() if user is None else {m["domain_id"] for m in gov.store.list_memberships(user["id"])}
        domains = frozenset(d["id"] for d in enabled if admin or d["id"] in memberships)
        home = user["home_domain_id"] if user else None
        domain = resolve_domain(request.headers.get("x-domain-id"), page=page, bootstrap=bootstrap,
                                domains=domains, home_domain_id=home, enabled_domains=enabled)
        permissions = frozenset(BUILTIN_PERMISSIONS if bootstrap else gov.effective_permissions(user["id"], domain))
        request.state.identity_trace_id = claims.get("jti") if claims else None
        return Identity(user["id"] if user else None, user["username"] if user else None,
                        "bootstrap" if bootstrap else user["user_type"], admin, home, domains, domain, permissions, mode)

    @staticmethod
    def _loopback(request: Request) -> bool:
        try:
            return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            return False  # Host / Forwarded headers never establish locality.


def get_identity(request: Request) -> Identity:
    return request.app.state.identity_resolver.resolve(request, page=not request.url.path.startswith("/api/v1/"))


def get_page_identity(request: Request) -> Identity:
    """Page-surface dependency used by the chat data routes."""
    return request.app.state.identity_resolver.resolve_page(request)


def get_bearer_identity(request: Request) -> Identity:
    return request.app.state.identity_resolver.resolve(request, bearer_only=True)


def register(app, config):
    app.state.identity_resolver = IdentityResolver(app, config)

    @app.exception_handler(IdentityError)
    async def identity_error(request, error):
        return JSONResponse({"detail": error.detail, "code": error.code}, status_code=error.status_code)
