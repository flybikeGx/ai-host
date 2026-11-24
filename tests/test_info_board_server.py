"""Real Streamable HTTP MCP: handshake, five tools, budget and auth failures."""
import asyncio
import json
import time

import httpx
import pytest

from agent_base.config import PROJECT
from agent_base.mcp_packages import Installations, load_package
from agent_base.mcp_runtime import PackageRuntime, issue_grant

PACKAGE = PROJECT / "first_party/info_board"


@pytest.fixture
def running(config):
    package = load_package(PACKAGE)
    Installations(config.data / "mcp").install(package)
    runtime = PackageRuntime(config, package)
    status = runtime.ensure()
    yield runtime, status
    runtime.stop()


def grant(runtime):
    return issue_grant(runtime.control_token(), actor="repo:server", expires=time.time() + 120)


def test_real_mcp_handshake_and_all_five_tools(running):
    runtime, status = running
    token = grant(runtime)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def exercise():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                     trust_env=False) as client:
            async with streamable_http_client(status["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "info_create", "info_list", "info_get", "info_update", "info_delete"}

                    async def invoke(name, arguments):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, result
                        return json.loads(result.content[0].text)

                    created = await invoke("info_create", {"title": "真实 MCP", "body_markdown": "# 正文",
                                                           "tags": ["mcp"]})
                    assert created["author"] == "repo:server"
                    listing = await invoke("info_list", {"query": "真实"})
                    assert listing["items"][0]["id"] == created["id"]
                    assert "body_markdown" not in listing["items"][0]
                    assert (await invoke("info_get", {"id": created["id"]}))["body_markdown"] == "# 正文"
                    assert (await invoke("info_update", {"id": created["id"], "title": "更新",
                                                         "expected_updated": created["updated"]}))["title"] == "更新"
                    await invoke("info_delete", {"id": created["id"]})
                    missing = await session.call_tool("info_get", {"id": created["id"]})
                    assert missing.isError
                    assert json.loads(missing.content[0].text)["status"] == 404

    asyncio.run(exercise())


def test_real_mcp_list_budget(running):
    runtime, status = running
    token = grant(runtime)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def exercise():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                     trust_env=False) as client:
            async with streamable_http_client(status["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    for index in range(25):
                        await session.call_tool("info_create", {
                            "title": f"预算 {index}", "body_markdown": "z" * 1200})
                    result = await session.call_tool("info_list", {"limit": 50})
                    assert not result.isError
                    text = result.content[0].text
                    assert len(text) <= 8000
                    assert json.loads(text)["next_offset"] is not None

    asyncio.run(exercise())


def test_wrong_token_and_browser_origin_rejected(running):
    _, status = running
    endpoint = status["endpoint"]
    assert httpx.get(endpoint.removesuffix("/mcp") + "/health",
                     headers={"Authorization": "Bearer wrong"}, trust_env=False).status_code == 401
    response = httpx.post(endpoint, headers={"Authorization": "Bearer wrong",
                                             "Content-Type": "application/json",
                                             "Accept": "application/json, text/event-stream"},
                          json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                          trust_env=False)
    assert response.status_code == 401
    origin = httpx.post(endpoint, headers={"Authorization": "Bearer wrong", "Origin": "http://evil.example"},
                        json={}, trust_env=False)
    assert origin.status_code == 403


def test_restart_keeps_data(config):
    package = load_package(PACKAGE)
    Installations(config.data / "mcp").install(package)
    runtime = PackageRuntime(config, package)
    try:
        runtime.ensure()
        from first_party.info_board.backend.store import InfoBoard
        post = InfoBoard(runtime.root).create("重启", "数据仍在")
        runtime.stop()
        status = runtime.ensure(explicit=True)
        assert status["state"] == "healthy"
        assert InfoBoard(runtime.root).get(post["id"])["body_markdown"] == "数据仍在"
    finally:
        runtime.stop()
