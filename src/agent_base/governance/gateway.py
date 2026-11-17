"""治理层中央网关：唯一收口，反向代理 MCP + 统一调用入口 + 双层审计。

职责（GOVERNANCE_DESIGN.md I-7 / S-6，AIHub 设计文档 §5.1）：

- ``call``：Web/编排统一入口（``service/tool/arguments`` 参数化），鉴权在 web 层完成，此处做
  RBAC（``tool.execute``，deny 优先 + 域/组收窄）→ 解析服务 → 凭据注入 → 经 ``ServiceClient``
  调用后端工具 → 写网关级审计（请求 + 响应，``trace_id`` 关联）。
- ``relay``：反向代理路径（中继 MCP Streamable HTTP），**不终结 MCP 会话、不重建 session**，只在
  proxy 边界 inspect JSON-RPC envelope，注入 ``trace_id``、校验工具权限、写请求审计。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from .registry import ServiceRegistry
from .service import Governance


class Gateway:
    def __init__(self, gov: Governance, registry: ServiceRegistry):
        self.gov = gov
        self.registry = registry

    def _audit(self, service: dict, user_id: str, trace_id: str, action: str, detail: dict):
        self.gov.store.audit(domain_id=service["domain_id"], user_id=user_id, trace_id=trace_id,
                             level="gateway", action=action, detail=detail)

    # ---- 统一调用入口 ----
    def call(self, *, service_id: str, tool: str, arguments: dict, user_id: str,
             trace_id: str | None = None, domain_id: str | None = None,
             group_id: str | None = None) -> dict:
        service = self.registry.store.get_service(service_id)
        if service["status"] != "active":
            raise PermissionError("服务不可用")
        if domain_id is not None and domain_id != service["domain_id"]:
            raise PermissionError("越域访问")
        trace_id = trace_id or uuid.uuid4().hex
        scope_group = group_id if group_id is not None else service["group_id"]
        if not self.gov.authorize(user_id, "tool.execute", service["domain_id"], scope_group):
            self._audit(service, user_id, trace_id, "gateway.call", {"tool": tool, "decision": "deny"})
            raise PermissionError("缺少权限：tool.execute")
        if tool not in {t["name"] for t in self.registry.store.list_tools(service_id)}:
            self._audit(service, user_id, trace_id, "gateway.call",
                        {"tool": tool, "decision": "deny", "reason": "tool_not_found"})
            raise ValueError(f"工具不在服务目录：{tool}")
        self._audit(service, user_id, trace_id, "gateway.call", {"tool": tool, "decision": "allow"})
        started = time.time()
        try:
            result = asyncio.run(self.registry.client.call_tool(service, tool, arguments, trace_id=trace_id))
        except Exception as error:
            self._audit(service, user_id, trace_id, "gateway.call.result",
                        {"tool": tool, "outcome": "error", "error": f"{type(error).__name__}: {str(error)[:200]}"})
            raise
        self._audit(service, user_id, trace_id, "gateway.call.result",
                    {"tool": tool, "outcome": "error" if result["is_error"] else "ok",
                     "latency_ms": round((time.time() - started) * 1000, 1)})
        return {"trace_id": trace_id, "service": service["name"], "tool": tool,
                "is_error": result["is_error"], "content": result["content"]}

    # ---- 反向代理路径 ----
    def _inspect_envelope(self, body: bytes, trace_id: str) -> tuple[bytes, list[str]]:
        """解析 JSON-RPC envelope：注入 ``_meta.trace_id``，返回 (重编码 body, tools/call 的工具名)。"""
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return body, []
        messages = payload if isinstance(payload, list) else [payload]
        tools = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            params = message.get("params")
            if isinstance(params, dict):
                params["_meta"] = {**(params.get("_meta") or {}), "trace_id": trace_id}
                if message.get("method") == "tools/call" and isinstance(params.get("name"), str):
                    tools.append(params["name"])
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), tools

    async def relay(self, *, service_id: str, user_id: str, request: Request,
                    trace_id: str | None = None) -> StreamingResponse:
        service = self.registry.store.get_service(service_id)
        if service["transport"] != "remote":
            raise PermissionError("反向代理仅支持 remote 服务")
        if service["status"] != "active":
            raise PermissionError("服务不可用")
        if not self.gov.authorize(user_id, "service.read", service["domain_id"], service["group_id"]):
            self._audit(service, user_id, trace_id or "", "gateway.relay", {"decision": "deny"})
            raise PermissionError("缺少权限：service.read")
        trace_id = trace_id or uuid.uuid4().hex

        body = await request.body()
        tools = []
        if request.method == "POST" and body:
            body, tools = self._inspect_envelope(body, trace_id)
            for tool in tools:
                if not self.gov.authorize(user_id, "tool.execute", service["domain_id"], service["group_id"]):
                    self._audit(service, user_id, trace_id, "gateway.relay",
                                {"tool": tool, "decision": "deny"})
                    raise PermissionError("缺少权限：tool.execute")
        self._audit(service, user_id, trace_id, "gateway.relay",
                    {"method": request.method, "tools": tools, "decision": "allow"})

        # 转发头：剥离用户 token，注入服务凭据 + trace_id；透传 MCP 协议头。
        forward_headers = {}
        for name in ("content-type", "accept", "mcp-protocol-version", "mcp-session-id"):
            if name in request.headers:
                forward_headers[name] = request.headers[name]
        forward_headers["x-trace-id"] = trace_id
        credential = self.registry.client.credential(service)
        if credential is not None:
            forward_headers["authorization"] = "Bearer " + credential

        upstream = httpx.AsyncClient(timeout=60, trust_env=False)
        req = upstream.build_request(request.method, service["endpoint"], content=body or None,
                                     headers=forward_headers)
        response = await upstream.send(req, stream=True)

        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await upstream.aclose()

        headers = {"x-trace-id": trace_id}
        if response.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = response.headers["mcp-session-id"]
        return StreamingResponse(stream(), status_code=response.status_code,
                                 media_type=response.headers.get("content-type"), headers=headers)
