"""治理层：服务注册中心（registry）与中央网关（gateway）的框架验证。

覆盖：服务/工具/健康检查 CRUD、embedded/remote 双模传输、网关统一调用入口的
RBAC（deny 优先 + 越域拒绝）与双层审计（trace_id 关联）。
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from agent_base.governance.gateway import Gateway
from agent_base.governance.registry import ServiceClient, ServiceRegistry
from agent_base.governance.service import Governance

ECHO = Path(__file__).parent / "fixtures" / "echo_mcp.py"


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


def _echo_app():
    """回显 body 与关键头的 FastAPI 应用，充当 relay 的 remote 后端。"""
    app = FastAPI()

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def echo(request: Request):
        body = await request.body()
        return {"body": body.decode(), "auth": request.headers.get("authorization"),
                "trace": request.headers.get("x-trace-id")}

    return app


def _gov(config, password="pw"):
    gov = Governance(config)
    gov.bootstrap(admin_password=password)
    return gov


def _embedded_endpoint():
    return {"command": sys.executable, "args": [str(ECHO)]}


def _echo_service(gov, *, domain_id=None, credential_ref=None):
    registry = ServiceRegistry(gov.store)
    domain_id = domain_id or gov.list_domains()[0]["id"]
    service = registry.register(domain_id, "echo", "embedded", _embedded_endpoint(),
                                credential_ref=credential_ref)
    registry.set_tools(service["id"], [{"name": "echo", "description": "回显",
                                        "input_schema": {"type": "object",
                                                         "properties": {"text": {"type": "string"}}}}])
    return registry, service


# ---- 注册 / 目录 ----

def test_register_service_tools_and_server_json(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    assert service["transport"] == "embedded"

    described = registry.get(service["id"])
    assert described["tools"] == ["echo"]

    server_json = registry.server_json(service["id"])
    assert server_json["transport"] == "stdio"
    assert server_json["command"] == sys.executable
    assert server_json["tools"] == ["echo"]

    assert [s["name"] for s in registry.list(service["domain_id"])] == ["echo"]


def test_endpoint_validation(config):
    gov = _gov(config)
    registry = ServiceRegistry(gov.store)
    domain_id = gov.list_domains()[0]["id"]
    with pytest.raises(ValueError, match="http"):
        registry.register(domain_id, "bad", "remote", "not-a-url")
    with pytest.raises(ValueError, match="command"):
        registry.register(domain_id, "bad", "embedded", {"args": []})
    with pytest.raises(ValueError, match="凭据"):
        registry.register(domain_id, "bad", "remote", "https://user:pw@host.example")


def test_remove_service_cascades(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    registry.remove(service["id"])
    with pytest.raises(ValueError):
        registry.get(service["id"])
    assert registry.store.list_tools(service["id"]) == []


# ---- 网关统一调用 ----

def test_gateway_call_embedded_end_to_end(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    gateway = Gateway(gov, registry)
    admin = gov.store.get_user_by_username("admin")

    result = gateway.call(service_id=service["id"], tool="echo", arguments={"text": "hi"},
                          user_id=admin["id"], trace_id="t-123")
    assert result["is_error"] is False
    assert result["content"][0]["text"] == "echo:hi"
    assert result["trace_id"] == "t-123"

    # 双层审计：请求 + 响应，同一 trace_id
    entries = [a for a in gov.list_audit(trace_id="t-123")]
    actions = {a["action"] for a in entries}
    assert {"gateway.call", "gateway.call.result"} <= actions
    assert all(a["trace_id"] == "t-123" for a in entries)


def test_gateway_call_rbac_deny(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    gateway = Gateway(gov, registry)
    nobody = gov.create_user("nobody", password="pw")

    with pytest.raises(PermissionError, match="tool.execute"):
        gateway.call(service_id=service["id"], tool="echo", arguments={"text": "x"},
                     user_id=nobody["id"])
    denied = [a for a in gov.list_audit() if a["action"] == "gateway.call"]
    assert denied and denied[0]["detail"] == {"tool": "echo", "decision": "deny"}


def test_gateway_call_tool_not_found(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    gateway = Gateway(gov, registry)
    admin = gov.store.get_user_by_username("admin")
    with pytest.raises(ValueError, match="工具不在服务目录"):
        gateway.call(service_id=service["id"], tool="missing", arguments={}, user_id=admin["id"])


def test_gateway_call_cross_domain_rejected(config):
    gov = _gov(config)
    other = gov.create_domain("other", "别的域")
    registry, service = _echo_service(gov)
    gateway = Gateway(gov, registry)
    admin = gov.store.get_user_by_username("admin")
    with pytest.raises(PermissionError, match="越域"):
        gateway.call(service_id=service["id"], tool="echo", arguments={}, user_id=admin["id"],
                     domain_id=other["id"])


def test_gateway_call_missing_credential(config):
    gov = _gov(config)
    registry, service = _echo_service(gov, credential_ref="MISSING_SERVICE_TOKEN")
    gateway = Gateway(gov, registry)
    admin = gov.store.get_user_by_username("admin")
    with pytest.raises(ValueError, match="缺少服务凭据"):
        gateway.call(service_id=service["id"], tool="echo", arguments={}, user_id=admin["id"])


# ---- 双模传输 ----

def test_remote_transport_call(config):
    fastmcp = FastMCP("remote-fixture")

    @fastmcp.tool()
    def echo(text: str) -> str:
        return "echo:" + text

    port, stop = _serve(fastmcp.streamable_http_app())
    try:
        client = ServiceClient()
        service = {"transport": "remote", "endpoint": f"http://127.0.0.1:{port}/mcp",
                   "credential_ref": None}
        result = asyncio.run(client.call_tool(service, "echo", {"text": "hi"}, trace_id="t-1"))
        assert result["content"][0]["text"] == "echo:hi"
        assert client.credential(service) is None
    finally:
        stop()


# ---- 反向代理 ----

def test_relay_inspect_envelope_injects_trace_id(config):
    gov = _gov(config)
    gateway = Gateway(gov, ServiceRegistry(gov.store))
    body, tools = gateway._inspect_envelope(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "echo", "arguments": {}}}).encode(), "t-9")
    assert tools == ["echo"]
    decoded = json.loads(body)
    assert decoded["params"]["_meta"] == {"trace_id": "t-9"}


def test_relay_rbac_deny(config):
    gov = _gov(config)
    registry = ServiceRegistry(gov.store)
    service = registry.register(gov.list_domains()[0]["id"], "remote-echo", "remote",
                                "http://127.0.0.1:1/mcp")
    gateway = Gateway(gov, registry)
    nobody = gov.create_user("nobody3", password="pw")
    from fastapi import Request
    request = Request({"type": "http", "method": "POST", "headers": [],
                       "path": "/mcp", "query_string": b""})
    with pytest.raises(PermissionError, match="service.read"):
        asyncio.run(gateway.relay(service_id=service["id"], user_id=nobody["id"],
                                  request=request, trace_id="t-1"))


def test_http_relay_forwards_strips_user_token_injects_trace(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    port, stop = _serve(_echo_app())
    try:
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            token = client.post("/api/v1/auth/login",
                                json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
            h = {"Authorization": "Bearer " + token}
            domain_id = client.get("/api/v1/domains", headers=h).json()[0]["id"]
            svc = client.post("/api/v1/registry/services", headers=h,
                              json={"domain_id": domain_id, "name": "echo-relay", "transport": "remote",
                                    "endpoint": f"http://127.0.0.1:{port}/mcp"})
            service_id = svc.json()["id"]

            resp = client.post(f"/api/v1/gateway/services/{service_id}/mcp", headers=h,
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "echo", "arguments": {}}})
            assert resp.status_code == 200
            trace = resp.headers["x-trace-id"]
            data = resp.json()
            # trace_id 贯穿 header 与 envelope
            assert data["trace"] == trace
            decoded = json.loads(data["body"])
            assert decoded["params"]["_meta"]["trace_id"] == trace
            # 用户 JWT 不透传到后端
            assert data["auth"] is None
    finally:
        stop()


def test_health_check_records(config):
    gov = _gov(config)
    registry, service = _echo_service(gov)
    check = registry.check(service["id"])
    assert check["healthy"] is True
    assert check["tools"] == ["echo"]
    assert registry.store.list_checks(service["id"])[0]["healthy"] == 1


# ---- HTTP 端点 ----

def test_http_registry_and_gateway_routes(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        token = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
        h = {"Authorization": "Bearer " + token}

        created = client.post("/api/v1/registry/services", headers=h, json={
            "domain_id": client.get("/api/v1/domains", headers=h).json()[0]["id"],
            "name": "echo", "transport": "embedded", "endpoint": _embedded_endpoint()})
        assert created.status_code == 201
        service_id = created.json()["id"]

        assert client.put(f"/api/v1/registry/services/{service_id}/tools", headers=h,
                          json={"tools": [{"name": "echo", "description": "回显"}]}).status_code == 200
        assert client.get("/api/v1/registry/services", headers=h).status_code == 200
        assert client.get(f"/api/v1/registry/services/{service_id}/server-json",
                          headers=h).json()["tools"] == ["echo"]

        called = client.post("/api/v1/gateway/call", headers=h,
                             json={"service_id": service_id, "tool": "echo",
                                   "arguments": {"text": "http"}})
        assert called.status_code == 200
        assert called.json()["content"][0]["text"] == "echo:http"

        # 未授权用户调用 → 403
        nobody = client.post("/api/v1/users", headers=h, json={"username": "nobody2", "password": "pw"}).json()
        nh = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login", json={"username": "nobody2", "password": "pw"}).json()["token"]}
        assert client.post("/api/v1/gateway/call", headers=nh,
                           json={"service_id": service_id, "tool": "echo",
                                 "arguments": {}}).status_code == 403
