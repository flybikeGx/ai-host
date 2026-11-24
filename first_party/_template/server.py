"""Minimal Streamable HTTP MCP used by the first-party package template.

Copy ``first_party/_template`` to ``first_party/<id>/`` (id with ``-`` becomes
``_``), then replace this module's tools and ``mcp.yaml``. The service must keep
the control-plane contract: bind loopback only, serve ``/health`` for the
platform probe and ``/mcp`` for Streamable HTTP MCP.
"""
from __future__ import annotations

import argparse
import json
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route


def application(data: Path):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    server = Server("example-package")
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name="echo", description="回显输入文本",
                           inputSchema={"type": "object", "properties": {"text": {"type": "string"}},
                                        "required": ["text"], "additionalProperties": False})]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name != "echo":
            raise ValueError(f"未知工具：{name}")
        return [types.TextContent(type="text", text=json.dumps({"echo": arguments["text"]}, ensure_ascii=False))]

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    async def health(request):
        return JSONResponse({"ok": True, "package": "example-package"})

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    return Starlette(routes=[Route("/health", health),
                             Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()
    uvicorn.run(application(Path(args.data)), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
