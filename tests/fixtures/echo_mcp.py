"""Fixture：最小 stdio MCP 服务，供治理层网关 embedded 传输的端到端测试。"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-fixture")


@mcp.tool()
def echo(text: str) -> str:
    return "echo:" + text


if __name__ == "__main__":
    mcp.run(transport="stdio")
