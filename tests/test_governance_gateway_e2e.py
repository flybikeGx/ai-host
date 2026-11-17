"""治理层：中央网关 + 注册中心对模拟 MCP 服务器与模拟知识库服务的端到端验证。

与 ``test_governance_oss.py``（I-4）同理，本文件用 ``mocks/`` 包里两个可独立运行的
模拟 MCP 服务做真实网络端到端：注册到 registry → 经 ``gateway.call``（真实 MCP 协议往返，
ServiceClient 终结协议）调用工具 → 双层审计 trace_id 关联。覆盖：

- 模拟 MCP 服务器（echo/add）remote 双模经网关调用；
- 模拟知识库服务（knowledge_catalog/search/read/submit）契约与真实 ``knowledge_mcp.py`` 对齐；
- 健康检查、越权拒绝、反向代理 trace_id 注入。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_base.mocks.knowledge import knowledge_http_app
from agent_base.mocks.mcp import echo_http_app

ECHO_TOOLS = [{"name": "echo", "description": "回显"},
              {"name": "add", "description": "求和"}]
KB_TOOLS = [{"name": "knowledge_catalog", "description": "目录"},
            {"name": "knowledge_search", "description": "搜索"},
            {"name": "knowledge_read", "description": "读取"},
            {"name": "knowledge_submit", "description": "投稿"}]


def _serve(app):
    """在自由端口起一个 uvicorn 线程，返回 (port, stop)。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
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


def _register(client, headers, domain_id, name, transport, endpoint, tools):
    svc = client.post("/api/v1/registry/services", headers=headers,
                      json={"domain_id": domain_id, "name": name, "transport": transport,
                            "endpoint": endpoint}).json()
    client.put(f"/api/v1/registry/services/{svc['id']}/tools", headers=headers,
               json={"tools": tools})
    return svc["id"]


def test_gateway_e2e_echo_and_knowledge(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    echo_port, echo_stop = _serve(echo_http_app())
    kb_port, kb_stop = _serve(knowledge_http_app())
    try:
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            token = client.post("/api/v1/auth/login",
                                json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
            h = {"Authorization": "Bearer " + token}
            domain_id = client.get("/api/v1/domains", headers=h).json()[0]["id"]

            echo_id = _register(client, h, domain_id, "echo-mock", "remote",
                                f"http://127.0.0.1:{echo_port}/mcp", ECHO_TOOLS)
            kb_id = _register(client, h, domain_id, "knowledge-mock", "remote",
                              f"http://127.0.0.1:{kb_port}/mcp", KB_TOOLS)

            # 服务目录可见，健康检查真实 MCP list_tools
            services = client.get("/api/v1/registry/services", headers=h,
                                  params={"domain_id": domain_id}).json()
            assert {s["name"] for s in services} == {"echo-mock", "knowledge-mock"}
            check = client.post(f"/api/v1/registry/services/{kb_id}/check", headers=h).json()
            assert check["healthy"] is True
            assert set(check["tools"]) == {"knowledge_catalog", "knowledge_search",
                                           "knowledge_read", "knowledge_submit"}

            # ---- 模拟 MCP 服务器：echo / add ----
            echo = client.post("/api/v1/gateway/call", headers=h,
                               json={"service_id": echo_id, "tool": "echo",
                                     "arguments": {"text": "hi"}})
            assert echo.status_code == 200
            assert echo.json()["content"][0]["text"] == "echo:hi"
            trace_id = echo.json()["trace_id"]

            add = client.post("/api/v1/gateway/call", headers=h,
                              json={"service_id": echo_id, "tool": "add",
                                    "arguments": {"a": 2, "b": 3}}).json()
            assert add["content"][0]["text"] == "5"

            # ---- 模拟知识库服务：四类契约工具（dict 返回 → text 为 JSON） ----
            catalog = client.post("/api/v1/gateway/call", headers=h,
                                  json={"service_id": kb_id, "tool": "knowledge_catalog",
                                        "arguments": {}}).json()
            data = json.loads(catalog["content"][0]["text"])
            assert data["total"] == 3
            assert {d["namespace"] for d in data["items"]} == {"ops", "dev"}

            search = client.post("/api/v1/gateway/call", headers=h,
                                 json={"service_id": kb_id, "tool": "knowledge_search",
                                       "arguments": {"namespace": "ops", "query": "发布"}}).json()
            hits = json.loads(search["content"][0]["text"])["hits"]
            assert hits and hits[0]["document_id"] == "kb-ops-001"

            read = client.post("/api/v1/gateway/call", headers=h,
                               json={"service_id": kb_id, "tool": "knowledge_read",
                                     "arguments": {"document_id": "kb-ops-001",
                                                   "version_id": "v1", "view": "outline"}}).json()
            outline = json.loads(read["content"][0]["text"])
            assert outline["sections"] == ["准备", "发布", "回滚"]

            submit = client.post("/api/v1/gateway/call", headers=h,
                                 json={"service_id": kb_id, "tool": "knowledge_submit",
                                       "arguments": {"namespace": "ops", "operation": "create",
                                                     "candidate": {"title": "演练"},
                                                     "reason": "验证", "idempotency_key": "k-1"}}).json()
            accepted = json.loads(submit["content"][0]["text"])
            assert accepted["accepted"] is True
            assert accepted["submission_id"].startswith("kb-sub-")

            # ---- 双层审计：gateway.call + gateway.call.result，同一 trace_id ----
            entries = client.get("/api/v1/audit", headers=h,
                                 params={"trace_id": trace_id}).json()
            actions = {a["action"] for a in entries}
            assert {"gateway.call", "gateway.call.result"} <= actions
            assert all(a["trace_id"] == trace_id for a in entries)
    finally:
        echo_stop()
        kb_stop()


def test_gateway_e2e_unauthorized_knowledge_denied(config, monkeypatch):
    """无 tool.execute 权限的用户调用知识库服务 → 403。"""
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    kb_port, kb_stop = _serve(knowledge_http_app())
    try:
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            admin = {"Authorization": "Bearer " + client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]}
            domain_id = client.get("/api/v1/domains", headers=admin).json()[0]["id"]
            kb_id = _register(client, admin, domain_id, "knowledge-mock", "remote",
                              f"http://127.0.0.1:{kb_port}/mcp", KB_TOOLS)

            client.post("/api/v1/users", headers=admin,
                        json={"username": "nobody", "password": "pw"})
            nh = {"Authorization": "Bearer " + client.post(
                "/api/v1/auth/login", json={"username": "nobody", "password": "pw"}).json()["token"]}
            resp = client.post("/api/v1/gateway/call", headers=nh,
                               json={"service_id": kb_id, "tool": "knowledge_catalog",
                                     "arguments": {}})
            assert resp.status_code == 403
    finally:
        kb_stop()


def test_gateway_relay_injects_trace_into_knowledge_envelope(config, monkeypatch):
    """反向代理：注册 remote 知识库服务，relay 注入 trace_id、剥离用户 token。"""
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    echo_app = FastAPI()

    @echo_app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def echo(request: Request):
        body = await request.body()
        return {"body": body.decode(), "auth": request.headers.get("authorization"),
                "trace": request.headers.get("x-trace-id")}

    port, stop = _serve(echo_app)
    try:
        from agent_base.web import create_app
        with TestClient(create_app(config, background=False)) as client:
            token = client.post("/api/v1/auth/login",
                                json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
            h = {"Authorization": "Bearer " + token}
            domain_id = client.get("/api/v1/domains", headers=h).json()[0]["id"]
            svc = client.post("/api/v1/registry/services", headers=h,
                              json={"domain_id": domain_id, "name": "kb-relay", "transport": "remote",
                                    "endpoint": f"http://127.0.0.1:{port}/mcp"}).json()

            resp = client.post(f"/api/v1/gateway/services/{svc['id']}/mcp", headers=h,
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "knowledge_catalog", "arguments": {}}})
            assert resp.status_code == 200
            data = resp.json()
            trace = resp.headers["x-trace-id"]
            decoded = json.loads(data["body"])
            assert data["trace"] == trace
            assert decoded["params"]["_meta"]["trace_id"] == trace
            assert data["auth"] is None
    finally:
        stop()
