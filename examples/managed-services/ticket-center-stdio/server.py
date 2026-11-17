"""Shared ticket-center stdio MCP. Business credential arrives via env only."""
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

boot_id = secrets.token_hex(8)
server = Server("ticket-center")


def _load(data: Path, name: str) -> list[dict]:
    file = data / name
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
    data = Path(sys.argv[1])
    if not os.environ.get("TICKET_AUTH"):
        raise ValueError("缺少 TICKET_AUTH 业务凭据")
    if name == "ticket_search":
        query = arguments["query"].lower()
        results = [t for t in _load(data, "tickets.jsonl")
                   if query in t["title"].lower() or any(query in tag for tag in t["tags"])]
        if arguments.get("status"):
            results = [t for t in results if t["status"] == arguments["status"]]
        return [types.TextContent(type="text", text=json.dumps({
            "ok": True, "results": results[:20], "total": len(results),
            "auth_ok": True, "boot_id": boot_id, "pid": os.getpid()}, ensure_ascii=False))]
    if name == "ticket_subscribe":
        key = arguments["agent"] + "|" + arguments["project"]
        existing = _load(data, "subscriptions.jsonl")
        for item in existing:
            if item["key"] == key:
                return [types.TextContent(type="text", text=json.dumps(
                    {"ok": True, "subscription_id": item["subscription_id"], "duplicate": True,
                     "boot_id": boot_id}, ensure_ascii=False))]
        subscription = {"key": key, "subscription_id": "sub-" + secrets.token_hex(6),
                        "agent": arguments["agent"], "project": arguments["project"], "created": time.time()}
        with (data / "subscriptions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(subscription, ensure_ascii=False) + "\n")
        return [types.TextContent(type="text", text=json.dumps(
            {"ok": True, "subscription_id": subscription["subscription_id"], "duplicate": False,
             "boot_id": boot_id}, ensure_ascii=False))]
    raise ValueError("unknown tool")


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
