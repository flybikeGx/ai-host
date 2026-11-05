"""Persistent demo MCP. Equal tool names deliberately test repo namespaces."""
import asyncio
import json
import math
import os
import secrets
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

root = Path(__file__).resolve().parents[1]
domain = json.loads((root / "domain.json").read_text())
boot_id = secrets.token_hex(8)
calls = 0
server = Server(domain["name"])


@server.list_tools()
async def list_tools():
    return [types.Tool(name="analyze", description=domain["description"], inputSchema={
        "type": "object", "properties": {"values": {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 100}},
        "required": ["values"], "additionalProperties": False})]


@server.call_tool()
async def call_tool(name, arguments):
    global calls
    if name != "analyze":
        raise ValueError("unknown tool")
    values = arguments["values"]
    if domain["operation"] == "sum":
        value = sum(values)
    elif domain["operation"] == "product":
        value = math.prod(values)
    else:
        value = sum(values) / len(values)
    calls += 1
    result = {"domain": domain["name"], "operation": domain["operation"], "values": values,
              "value": value, "boot_id": boot_id, "pid": os.getpid(), "call_number": calls}
    return [types.TextContent(type="text", text=json.dumps(result))]


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
