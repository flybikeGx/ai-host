"""Same-origin browser sessions for the existing governance accounts.

The browser holds an opaque HttpOnly cookie. Only its hash is stored, so page
navigation does not expose governance tokens or the legacy management token.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .governance.store import BUILTIN_PERMISSIONS

COOKIE = "agent_base_session"
SESSION_SECONDS = 1200


class BrowserLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=4096)


class BrowserAuth:
    def __init__(self, governance):
        self.gov = governance
        with self.gov.store.connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS browser_sessions(
                hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL)""")

    def required(self) -> bool:
        # Preserve the account-free local workspace. Once accounts are created,
        # anonymous requests must not retain global management access.
        with self.gov.store.connection() as conn:
            return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def identity(self, request: Request) -> dict | None:
        cookie = request.cookies.get(COOKIE)
        if not cookie:
            return None
        with self.gov.store.connection() as conn:
            row = conn.execute(
                "SELECT s.user_id,s.expires FROM browser_sessions s JOIN users u ON u.id=s.user_id"
                " WHERE s.hash=? AND u.status='active'", (self.digest(cookie),)).fetchone()
        if not row or row["expires"] <= time.time():
            return None
        return {"sub": row["user_id"], "jti": "browser:" + self.digest(cookie), "exp": row["expires"]}

    def current(self, request: Request) -> dict:
        identity = self.identity(request)
        state = {"authenticated": False, "login_required": self.required(),
                 "sso": {"identity_verification_configured": bool(self.gov.oss),
                         "browser_login_available": False}}
        if identity:
            user = self.gov.principal(identity["sub"])
            permissions = self.gov.effective_permissions(identity["sub"], None)
            user["effective_permissions"] = permissions
            state.update(authenticated=True, user=user, expires_at=identity["exp"],
                         can_manage_workspace=set(BUILTIN_PERMISSIONS).issubset(permissions))
        return state

    def revoke(self, request: Request):
        cookie = request.cookies.get(COOKIE)
        if cookie:
            with self.gov.store.connection() as conn:
                conn.execute("DELETE FROM browser_sessions WHERE hash=?", (self.digest(cookie),))

    def require_workspace(self, request: Request):
        identity = self.identity(request)
        if not identity:
            raise HTTPException(401, "登录已过期，请重新登录")
        permissions = self.gov.effective_permissions(identity["sub"], None)
        # The legacy workspace exposes global management operations. Until its
        # objects are domain-scoped, only a full global administrator may enter.
        if not set(BUILTIN_PERMISSIONS).issubset(permissions):
            raise HTTPException(403, "当前账号没有全局工作台管理权限")


def register(app):
    browser = BrowserAuth(app.state.governance)
    app.state.browser_auth = browser

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/session")
    def session(request: Request):
        result = response(browser.current(request))
        if request.cookies.get(COOKIE) and not browser.identity(request):
            result.delete_cookie(COOKIE, path="/")
        return result

    @app.post("/api/auth/login")
    def login(body: BrowserLogin, request: Request):
        try:
            account = browser.gov.login(body.username.strip(), body.password)["user"]
        except ValueError as error:
            raise HTTPException(401, str(error))
        browser.revoke(request)
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with browser.gov.store.connection() as conn:
            conn.execute("DELETE FROM browser_sessions WHERE expires<=?", (now,))
            conn.execute("INSERT INTO browser_sessions VALUES(?,?,?,?)",
                         (browser.digest(secret), account["id"], now, now + SESSION_SECONDS))
        result = response({"authenticated": True})
        result.set_cookie(COOKIE, secret, max_age=SESSION_SECONDS, httponly=True,
                          secure=request.url.scheme == "https", samesite="lax", path="/")
        return result

    @app.post("/api/auth/logout")
    def logout(request: Request):
        browser.revoke(request)
        result = response({"authenticated": False})
        result.delete_cookie(COOKIE, path="/")
        return result
