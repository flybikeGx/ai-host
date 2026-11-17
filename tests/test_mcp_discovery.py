"""MCP discovery: inventory, per-agent connection configs and session grants."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_base.knowledge_service import KnowledgeService
from agent_base.service_catalog import ServiceCatalog
from agent_base.service_store import ServiceStore
from agent_base.web import create_app
from test_services_install import exercise, expect_rejected, install_deployment


@pytest.fixture
def client(config, managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-shared")
    ServiceCatalog(config).publish("ticket-shared")
    stores = ServiceStore(config.data)
    agent = stores.ensure_agent_identity(source="repo:" + "e" * 16, name="discovery-agent")
    with TestClient(create_app(config, background=False)) as app:
        yield app, deployment, stores, agent, config


def test_inventory_covers_deployments_and_knowledge(client):
    app, deployment, stores, agent, config = client
    servers = {item["id"]: item for item in app.get("/api/mcp").json()["servers"]}
    ticket = servers["ticket-shared"]
    assert ticket["kind"] == "managed" and ticket["transport"] == "streamable_http"
    assert ticket["status"] == "running" and ticket["url"] == deployment["endpoint"]
    assert ticket["stable_endpoint"] is True
    assert ticket["tools"] == ["ticket_search", "ticket_subscribe"]
    assert ticket["connectable"] is True and ticket["publish_state"] == "published"
    assert ticket["auth"]["mode"] == "session_grant"
    assert ticket["auth"]["credential"]["endpoint"] == "/api/mcp/grants"
    knowledge = servers["knowledge"]
    assert knowledge["kind"] == "knowledge" and knowledge["connectable"] is False
    assert knowledge["url"] is None  # legacy instance is not running in this fixture
    # Discovery never inlines credential values.
    assert "fixture-ticket-secret" not in json.dumps(servers)
    well = app.get("/.well-known/mcp.json").json()["servers"]
    assert any(item["url"] == deployment["endpoint"] for item in well)
    assert all(item["url"] for item in well)


def test_agent_config_renders_fragments_for_each_client(client):
    app, deployment, stores, agent, config = client
    app.post("/api/services/ticket-shared/bind", json={
        "agent_id": agent["id"], "alias": "tickets", "tools": ["ticket_search"]})
    for agent_name, section, marker in [
            ("opencode", "mcp", "Bearer {env:AGENT_BASE_MCP_TICKET_SHARED_AUTH}"),
            ("claude-code", "mcpServers", "Bearer ${AGENT_BASE_MCP_TICKET_SHARED_AUTH}")]:
        body = app.get("/api/mcp/config", params={"agent": agent_name}).json()
        entry = body["config"][section]["agent-base-ticket-shared"]
        assert entry["url"] == deployment["endpoint"]
        assert entry["headers"]["Authorization"] == marker
        credential = body["env"]["AGENT_BASE_MCP_TICKET_SHARED_AUTH"]
        assert credential["type"] == "grant" and credential["endpoint"] == "/api/mcp/grants"
        assert credential["body"]["target"] == "ticket-shared"
    dsh = app.get("/api/mcp/config", params={"agent": "dsh"}).json()
    row = dsh["config"][0]["insert"][0]
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"
    assert row["config"]["transport"] == "streamable-http"
    assert row["config"]["headers"]["Authorization"] == 'process.env["AGENT_BASE_MCP_TICKET_SHARED_AUTH"]'
    assert app.get("/api/mcp/config", params={"agent": "unknown"}).status_code == 400


def test_repo_namespaces_are_listed_as_platform_only(config, repositories, resource_repos):
    repositories.install(str(resource_repos["finance"]))
    with TestClient(create_app(config, background=False)) as app:
        servers = {item["id"]: item for item in app.get("/api/mcp").json()["servers"]}
        rows = [item for item in servers.values() if item["kind"] == "repo"]
        assert rows, "repo installation should appear in the inventory"
        row = rows[0]
        assert row["status"] == "running" and row["url"].endswith("/mcp")
        assert row["stable_endpoint"] is False
        assert row["connectable"] is False and "X-Agent-Run" in row["reason"]
        assert row["auth"]["credential"]["type"] == "file"
        # Platform-only namespaces are explained but never wired into configs.
        body = app.get("/api/mcp/config", params={"agent": "opencode"}).json()
        assert all(not name.startswith("agent-base-repo") for name in body["config"]["mcp"])
        listed = [item for item in body["servers"] if item["kind"] == "repo"]
        assert listed and all(not item["connectable"] and item["reason"] for item in listed)


def test_session_grant_connects_through_the_real_gateway_and_revokes(client):
    app, deployment, stores, agent, config = client
    app.post("/api/services/ticket-shared/bind", json={
        "agent_id": agent["id"], "alias": "tickets", "tools": ["ticket_search"]})
    grant = app.post("/api/mcp/grants", json={
        "target": "ticket-shared", "agent_id": agent["id"], "seconds": 300}).json()
    assert grant["authorization"].startswith("Bearer ")
    assert grant["tools"] == ["ticket_search"] and grant["alias"] == "tickets"
    token = grant["authorization"].removeprefix("Bearer ")
    answers = asyncio.run(exercise(deployment["endpoint"], token,
                                   [("ticket_search", {"query": "login"})]))
    assert not answers[0].isError
    # Unbinding drops outstanding grants at once: the same token now fails.
    binding = stores.bindings(agent_id=agent["id"], deployment_id="ticket-shared")[0]
    app.post("/api/services/bindings/%s/disable" % binding["id"])
    expect_rejected(deployment["endpoint"], token,
                    [("ticket_search", {"query": "login"})], "401")


def test_grant_endpoint_validates_target_state_and_binding(client):
    app, deployment, stores, agent, config = client
    assert app.post("/api/mcp/grants", json={"target": "missing"}).status_code == 400
    assert app.post("/api/mcp/grants", json={
        "target": "ticket-shared", "agent_id": agent["id"]}).status_code == 400
    app.post("/api/services/ticket-shared/bind", json={
        "agent_id": agent["id"], "alias": "tickets", "tools": ["ticket_search"]})
    grant = app.post("/api/mcp/grants", json={
        "target": "ticket-shared", "agent_id": agent["id"], "seconds": 10})
    assert grant.status_code == 422  # below the minimum lifetime


def test_legacy_knowledge_grant_issues_a_working_read_token(client):
    app, deployment, stores, agent, config = client
    KnowledgeService(config).ensure()
    inventory = {item["id"]: item for item in app.get("/api/mcp").json()["servers"]}
    knowledge = inventory["knowledge"]
    assert knowledge["status"] == "running" and knowledge["connectable"] is True
    assert knowledge["tools"]
    grant = app.post("/api/mcp/grants", json={"target": "knowledge", "seconds": 120}).json()
    assert grant["authorization"].startswith("Bearer ")
    assert grant["scope"]["submit_namespace_ids"] == []
    listing = asyncio.run(_list_tools(knowledge["url"], grant["authorization"].removeprefix("Bearer ")))
    assert set(grant["tools"]) <= {tool.name for tool in listing}


async def _list_tools(endpoint, token):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
        async with streamable_http_client(endpoint, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return (await session.list_tools()).tools
