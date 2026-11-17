"""Native HTTP ticket-center MCP. The platform gateway proxies to this private port."""
import argparse
import json
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

boot_id = secrets.token_hex(8)
server = Server("ticket-center-http")
_DATA = Path(".")


def _load(name: str) -> list[dict]:
    file = _DATA / name
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]


@server.list_tools()
async def list_tools():
    return [
        types.Tool(name="ticket_search", description="按关键词和状态查询工单（只读）。", inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                           "status": {"type": "string", "enum": ["open", "closed"]}},
            "required": ["query"], "additionalProperties": False}),
        types.Tool(name="ticket_subscribe", description="幂等登记工单订阅；同 (agent, project) 只登记一次。", inputSchema={
            "type": "object",
            "properties": {"agent": {"type": "string", "minLength": 1, "maxLength": 64},
                           "project": {"type": "string", "minLength": 1, "maxLength": 64}},
            "required": ["agent", "project"], "additionalProperties": False}),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    if name == "ticket_search":
        query = arguments["query"].lower()
        results = [t for t in _load("tickets.jsonl")
                   if query in t["title"].lower() or any(query in tag for tag in t["tags"])]
        if arguments.get("status"):
            results = [t for t in results if t["status"] == arguments["status"]]
        return [types.TextContent(type="text", text=json.dumps({
            "ok": True, "results": results[:20], "total": len(results),
            "auth_ok": True, "boot_id": boot_id, "pid": os.getpid()}, ensure_ascii=False))]
    if name == "ticket_subscribe":
        key = arguments["agent"] + "|" + arguments["project"]
        for item in _load("subscriptions.jsonl"):
            if item["key"] == key:
                return [types.TextContent(type="text", text=json.dumps(
                    {"ok": True, "subscription_id": item["subscription_id"], "duplicate": True,
                     "boot_id": boot_id}, ensure_ascii=False))]
        subscription = {"key": key, "subscription_id": "sub-" + secrets.token_hex(6),
                        "agent": arguments["agent"], "project": arguments["project"], "created": time.time()}
        with (_DATA / "subscriptions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(subscription, ensure_ascii=False) + "\n")
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "subscription_id": subscription["subscription_id"], "duplicate": False,
             "boot_id": boot_id}, ensure_ascii=False))]
    raise ValueError("unknown tool")


@asynccontextmanager
async def lifespan(app):
    async with app.state.manager.run():
        yield


async def health(request: Request):
    return JSONResponse({"ok": True, "pid": os.getpid(), "boot_id": boot_id})


def build_app():
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    class MCPRoute:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    app = Starlette(routes=[Route("/health", health), Route("/mcp", MCPRoute(), methods=["GET", "POST", "DELETE"])],
                    lifespan=lifespan)
    app.state.manager = manager

    async def guard(scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            expected = os.environ.get("TICKET_HTTP_AUTH", "")
            if request.url.path == "/mcp" and not secrets.compare_digest(
                    request.headers.get("authorization", ""), expected):
                await JSONResponse({"error": "ticket auth required"}, status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)

    return guard


def main():
    global _DATA
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()
    _DATA = Path(args.data)
    if not os.environ.get("TICKET_HTTP_AUTH"):
        raise SystemExit("缺少 TICKET_HTTP_AUTH 业务凭据")
    uvicorn.Server(uvicorn.Config(build_app(), host=args.host, port=args.port, log_level="warning",
                                  lifespan="on", timeout_graceful_shutdown=5)).run()


if __name__ == "__main__":
    main()
