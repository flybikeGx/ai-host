"""服务注册中心：``service/service_tool/service_check`` + ``ServiceClient`` 双模传输。

与执行层 ``service_catalog.py``/``service_store.py``（共享 MCP 部署目录）职责分离：本模块是
治理控制面的服务目录，``transport=embedded|remote`` 双模，供中央网关路由与健康检查使用，
数据落在控制面库 ``control.sqlite``（见 GOVERNANCE_DESIGN.md §7.3 / S-3）。

凭据注入（S-10）：``credential_ref`` 只存环境变量名，值在调用时从进程环境解析，
绝不落库明文；remote 注入 ``Authorization: Bearer``，embedded 注入子进程环境变量。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .store import ControlStore


class ServiceClient:
    """embedded/remote 双模 MCP 客户端，只在网关自发调用（健康检查、``call_tool``）时终结协议。

    代理路径（反向代理）不终结 MCP 会话，见 ``gateway.py``。
    ``http_transport`` 仅供测试注入 ASGI transport，避免起真实网络服务。
    """

    def __init__(self, *, http_transport=None):
        self.http_transport = http_transport

    def credential(self, service: dict) -> str | None:
        ref = service.get("credential_ref")
        if not ref:
            return None
        value = os.environ.get(ref)
        if not value:
            raise ValueError(f"缺少服务凭据环境变量：{ref}")
        return value

    def _minimal_env(self, service: dict) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", "")}
        ref = service.get("credential_ref")
        credential = self.credential(service)
        if credential is not None and ref:
            env[ref] = credential
        return env

    def _remote_context(self, service: dict):
        headers = {}
        credential = self.credential(service)
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential
        client = httpx.AsyncClient(headers=headers, timeout=30, trust_env=False, transport=self.http_transport)
        return streamable_http_client(service["endpoint"], http_client=client)

    def _embedded_context(self, service: dict):
        spec = json.loads(service["endpoint"]) if service["endpoint"] else {}
        command = spec.get("command")
        if not command:
            raise ValueError("embedded 服务的 endpoint 必须为含 command 的 JSON")
        params = StdioServerParameters(command=command, args=spec.get("args") or [],
                                       env=self._minimal_env(service))
        return stdio_client(params)

    @asynccontextmanager
    async def _session(self, service: dict):
        context = self._embedded_context(service) if service["transport"] == "embedded" \
            else self._remote_context(service)
        async with context as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self, service: dict) -> list[str]:
        async with self._session(service) as session:
            listing = await session.list_tools()
            return [tool.name for tool in listing.tools]

    async def call_tool(self, service: dict, tool: str, arguments: dict,
                        *, trace_id: str | None = None) -> dict:
        async with self._session(service) as session:
            result = await session.call_tool(tool, arguments,
                                             meta={"trace_id": trace_id} if trace_id else None)
            content = []
            for item in result.content:
                if hasattr(item, "text"):
                    content.append({"type": "text", "text": item.text})
                else:
                    content.append({"type": type(item).__name__})
            return {"is_error": bool(result.isError), "content": content}

    async def probe(self, service: dict) -> dict:
        started = time.time()
        try:
            tools = await self.list_tools(service)
            return {"ok": True, "latency_ms": (time.time() - started) * 1000, "tools": tools}
        except Exception as error:  # 探测失败不抛出，落库为不健康
            return {"ok": False, "latency_ms": (time.time() - started) * 1000,
                    "error": f"{type(error).__name__}: {str(error)[:200]}", "tools": []}


class ServiceRegistry:
    """治理面服务目录门面：注册/发现/健康检查/``server.json``，依赖 ``ControlStore``。"""

    def __init__(self, store: ControlStore):
        self.store = store
        self.client = ServiceClient()

    # ---- 端点规范化 ----
    def _normalize_endpoint(self, transport: str, endpoint) -> str:
        if transport == "remote":
            if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
                raise ValueError("remote 服务的 endpoint 必须是 http(s) URL")
            parsed = urlparse(endpoint)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("remote endpoint 不得包含凭据、查询或片段")
            return endpoint
        spec = endpoint if isinstance(endpoint, dict) else (json.loads(endpoint) if endpoint else {})
        if not spec.get("command"):
            raise ValueError("embedded 服务的 endpoint 必须提供 command")
        return json.dumps(spec, ensure_ascii=False)

    def _audit(self, domain_id, actor, trace_id, action, detail):
        self.store.audit(domain_id=domain_id, user_id=actor, trace_id=trace_id, level="gateway",
                         action=action, detail=detail)

    # ---- 注册 / 目录 ----
    def register(self, domain_id: str, name: str, transport: str = "remote", endpoint="", *,
                 group_id: str | None = None, credential_ref: str | None = None, description: str = "",
                 actor: str | None = None, trace_id: str | None = None) -> dict:
        endpoint = self._normalize_endpoint(transport, endpoint)
        service = self.store.create_service(domain_id, name, transport, endpoint, group_id=group_id,
                                            credential_ref=credential_ref, description=description)
        self._audit(domain_id, actor, trace_id, "service.register",
                    {"service_id": service["id"], "name": name, "transport": transport})
        return service

    def describe(self, service: dict) -> dict:
        service = dict(service)
        service["tools"] = [t["name"] for t in self.store.list_tools(service["id"])]
        service["checks"] = self.store.list_checks(service["id"], limit=1)
        return service

    def list(self, domain_id: str | None = None) -> list[dict]:
        return [self.describe(s) for s in self.store.list_services(domain_id)]

    def get(self, service_id: str) -> dict:
        return self.describe(self.store.get_service(service_id))

    def resolve(self, *, service_id: str | None = None, name: str | None = None,
                domain_id: str | None = None) -> dict:
        if service_id:
            return self.store.get_service(service_id)
        if name and domain_id:
            service = self.store.get_service_by_name(domain_id, name)
            if service is None:
                raise ValueError(f"服务不存在：{name}")
            return service
        raise ValueError("需提供 service_id 或 (name + domain_id)")

    def update(self, service_id: str, *, actor: str | None = None, trace_id: str | None = None,
               **fields) -> dict:
        service = self.store.get_service(service_id)
        if "endpoint" in fields:
            transport = fields.get("transport", service["transport"])
            fields["endpoint"] = self._normalize_endpoint(transport, fields["endpoint"])
        updated = self.store.update_service(service_id, **fields)
        self._audit(updated["domain_id"], actor, trace_id, "service.update",
                    {"service_id": service_id, "fields": sorted(fields)})
        return updated

    def remove(self, service_id: str, *, actor: str | None = None, trace_id: str | None = None):
        service = self.store.get_service(service_id)
        self.store.delete_service(service_id)
        self._audit(service["domain_id"], actor, trace_id, "service.remove",
                    {"service_id": service_id, "name": service["name"]})

    # ---- 工具清单 ----
    def set_tools(self, service_id: str, tools: list[dict], *, actor: str | None = None,
                  trace_id: str | None = None) -> list[dict]:
        service = self.store.get_service(service_id)
        seen = set()
        for tool in tools:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("工具必须提供非空 name")
            if name in seen:
                raise ValueError(f"工具名重复：{name}")
            seen.add(name)
        self.store.set_tools(service_id, tools)
        self._audit(service["domain_id"], actor, trace_id, "service.tools",
                    {"service_id": service_id, "tools": sorted(seen)})
        return self.store.list_tools(service_id)

    def list_tools(self, service_id: str) -> list[dict]:
        return self.store.list_tools(service_id)

    # ---- 健康检查 ----
    def check(self, service_id: str, *, actor: str | None = None, trace_id: str | None = None) -> dict:
        service = self.store.get_service(service_id)
        result = asyncio.run(self.client.probe(service))
        check = self.store.record_check(service_id, result["ok"], result.get("latency_ms"),
                                        result.get("error", ""))
        self._audit(service["domain_id"], actor, trace_id, "service.check",
                    {"service_id": service_id, "healthy": result["ok"]})
        return {**check, "tools": result.get("tools", [])}

    # ---- server.json ----
    def server_json(self, service_id: str) -> dict:
        service = self.store.get_service(service_id)
        tools = [t["name"] for t in self.store.list_tools(service_id)]
        if service["transport"] == "remote":
            return {"name": service["name"], "transport": "streamable-http", "url": service["endpoint"],
                    "tools": tools}
        spec = json.loads(service["endpoint"]) if service["endpoint"] else {}
        return {"name": service["name"], "transport": "stdio", "command": spec.get("command"),
                "args": spec.get("args", []), "tools": tools}
