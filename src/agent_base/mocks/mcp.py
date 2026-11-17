"""模拟通用 MCP 服务器（``echo`` / ``add``），用于端到端验证网关与注册中心。

与生产代码解耦的假服务：既可用 streamable-http（remote）独立运行，也可 stdio（embedded），
验证 ``gateway.call`` 的 embedded/remote 双模与反向代理（``gateway.relay``）。

运行：``python -m agent_base mcp-mock --port 8891``（或直接 ``agent-base mcp-mock``）。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def create_echo_mcp() -> FastMCP:
    mcp = FastMCP("echo-mock")

    @mcp.tool()
    def echo(text: str) -> str:
        """回显文本。"""
        return "echo:" + text

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """整数求和。"""
        return a + b

    return mcp


def echo_http_app():
    """echo 模拟 MCP 的 streamable-http ASGI app（挂载于 ``/mcp``）。"""
    return create_echo_mcp().streamable_http_app()


def serve_echo(host: str = "127.0.0.1", port: int = 8891):
    import uvicorn

    uvicorn.run(echo_http_app(), host=host, port=port)


if __name__ == "__main__":
    serve_echo()
