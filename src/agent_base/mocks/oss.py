"""模拟企业 OSS 统一登录服务（REST），用于本地验证 I-4 后端。

模拟真实 IDaaS 的两段式：``/auth/login`` 用工号+密码换 token（密码只在 OSS 侧，
平台不经手），``/auth/getUserDetail`` 凭 token 回查身份（工号/部门/区域）。
**演示目录为假数据，不承载真实凭据。**

运行：``python -m agent_base oss-mock --port 8890``（或直接 ``agent-base oss-mock``）。
"""
from __future__ import annotations

import secrets
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# 演示目录：工号 -> 密码/部门/区域（仅验证用，非真实账号）。
DIRECTORY = {
    "w000001": {"password": "demo-pass-1", "departmentPath": "平台部/基础设施", "region": "y"},
    "w000002": {"password": "demo-pass-2", "departmentPath": "研发部/Agent 平台", "region": "g"},
    "w000003": {"password": "demo-pass-3", "departmentPath": "安全部/合规", "region": "b"},
}

_TOKEN_TTL = 3600
_tokens: dict[str, dict] = {}  # token -> {w3account, expires_at}


class LoginInput(BaseModel):
    account: str
    password: str


def create_app() -> FastAPI:
    app = FastAPI(title="OSS Mock 统一登录")

    @app.post("/auth/login")
    def login(body: LoginInput):
        entry = DIRECTORY.get(body.account)
        if entry is None or entry["password"] != body.password:
            raise HTTPException(401, "工号或密码错误")
        token = secrets.token_urlsafe(32)
        _tokens[token] = {"w3account": body.account, "expires_at": time.time() + _TOKEN_TTL}
        return {"token": token, "expires_in": _TOKEN_TTL}

    @app.post("/auth/getUserDetail")
    def get_user_detail(x_auth_token: str | None = Header(default=None)):
        if not x_auth_token:
            raise HTTPException(401, "缺少 X-Auth-Token")
        session = _tokens.get(x_auth_token)
        if session is None or session["expires_at"] < time.time():
            raise HTTPException(401, "token 无效或已过期")
        entry = DIRECTORY[session["w3account"]]
        return {"data": {"w3account": session["w3account"],
                         "departmentPath": entry["departmentPath"],
                         "region": entry["region"]}}

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def serve(host: str = "127.0.0.1", port: int = 8890):
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    serve()
