"""治理层：企业内网账号（OSS 统一登录）验证后端（I-4）端到端验证。

覆盖：OSS token → 工号回查 → 首次自动建号（低权 viewer）→ 签发 JWT → RBAC 按工号赋权；
密码只在模拟 OSS 侧校验，平台不经手密码。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_base.config import Config
from agent_base.mocks.oss import create_app as oss_app

FAKE = Path(__file__).parent / "fixtures" / "fake_dsh.py"


def _serve(app):
    """在自由端口起一个 uvicorn 线程，返回 (port, stop)。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    def stop():
        server.should_exit = True
        thread.join(timeout=5)

    return port, stop


def _oss_config(tmp_path: Path, endpoint: str) -> Config:
    file = tmp_path / "config.toml"
    file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\nengine="dsh"\ncommand='
                    + json.dumps([sys.executable, str(FAKE)])
                    + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=15\n'
                    + f'[oss]\nendpoint={json.dumps(endpoint)}\n')
    return Config.load(file)


def _oss_token(port: int, account: str, password: str) -> str:
    resp = httpx.post(f"http://127.0.0.1:{port}/auth/login",
                      json={"account": account, "password": password}, timeout=5)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_oss_login_first_time_auto_provision(tmp_path):
    port, stop = _serve(oss_app())
    try:
        config = _oss_config(tmp_path, f"http://127.0.0.1:{port}")
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            token = _oss_token(port, "w000001", "demo-pass-1")
            resp = client.post("/api/v1/auth/oss", json={"token": token})
            assert resp.status_code == 200, resp.text
            session = resp.json()
            assert session["user"]["huawei_w3account"] == "w000001"
            jwt = session["token"]
            home = session["user"]["home_domain_id"]

            # 首次自动建号 = 低权 viewer（域内）：只读可用，管理接口被拒
            me = client.get("/api/v1/me", headers={"Authorization": "Bearer " + jwt}).json()
            assert any(r["role_name"] == "viewer" for r in me["roles"])
            assert home in {d["domain_id"] for d in me["domains"]}

            h = {"Authorization": "Bearer " + jwt, "x-domain-id": home}
            assert client.get("/api/v1/registry/services", headers=h).status_code == 200
            assert client.get("/api/v1/users", headers=h).status_code == 403

            # 二次登录（新 token）复用同一工号，不重复建号
            token2 = _oss_token(port, "w000001", "demo-pass-1")
            again = client.post("/api/v1/auth/oss", json={"token": token2}).json()
            assert again["user"]["id"] == session["user"]["id"]
    finally:
        stop()


def test_oss_login_invalid_token(tmp_path):
    port, stop = _serve(oss_app())
    try:
        config = _oss_config(tmp_path, f"http://127.0.0.1:{port}")
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            resp = client.post("/api/v1/auth/oss", json={"token": "bogus-token"})
            assert resp.status_code == 401
    finally:
        stop()


def test_oss_login_rejected_at_oss_side(tmp_path):
    port, stop = _serve(oss_app())
    try:
        # 错误密码在 OSS 侧即被拒，拿不到 token
        resp = httpx.post(f"http://127.0.0.1:{port}/auth/login",
                          json={"account": "w000001", "password": "wrong"}, timeout=5)
        assert resp.status_code == 401
    finally:
        stop()


def test_oss_login_unconfigured(config):
    """未配置 oss.endpoint 时，OSS 登录后端显式报「未配置」。"""
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        resp = client.post("/api/v1/auth/oss", json={"token": "whatever"})
        assert resp.status_code == 401
        assert "未配置" in resp.json()["detail"]
