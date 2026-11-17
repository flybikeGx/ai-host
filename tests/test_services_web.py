"""002-D2: shared-service management API and state reporting."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from agent_base.service_catalog import ServiceCatalog
from agent_base.service_install import ManagedServices
from agent_base.service_store import ServiceStore
from agent_base.web import create_app
from test_services_install import install_deployment


@pytest.fixture
def client(config, managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-web")
    ServiceCatalog(config).publish("ticket-web")
    stores = ServiceStore(config.data)
    agent = stores.ensure_agent_identity(source="repo:" + "d" * 16, name="triage-agent")
    with TestClient(create_app(config, background=False)) as app:
        yield app, deployment, stores, agent


def test_services_list_and_detail_expose_state_bindings_and_metrics(client):
    app, deployment, stores, agent = client
    listing = app.get("/api/services").json()
    assert listing["supervisor"]["state"] in {"never_run", "running", "stale"}
    assert listing["guardian"]["state"] == "disabled"
    assert "limits" in listing and listing["limits"]["note"]
    row = next(item for item in listing["services"] if item["deployment_id"] == "ticket-web")
    assert row["publish_state"] == "published" and row["desired_state"] == "running"
    assert row["verification"] == "business_verified"
    assert row["contract"] == "ticket-center/v1"
    assert row["endpoint_kind"] == "managed"
    assert "resources" in row and row["metrics_state"] in {"measured", "unattributed"}
    # Bind through the API, then verify the list names the bound Agent.
    binding = app.post("/api/services/ticket-web/bind", json={
        "agent_id": agent["id"], "alias": "tickets", "tools": ["ticket_search"]}).json()
    assert binding["enabled"] and binding["revision"] == 1
    row = next(item for item in app.get("/api/services").json()["services"]
               if item["deployment_id"] == "ticket-web")
    assert row["bound_agents"][0]["name"] == "triage-agent"
    detail = app.get("/api/services/ticket-web").json()
    assert detail["health"]["gateway_alive"] and detail["health"]["ready"]
    assert detail["bindings"][0]["agent_name"] == "triage-agent"
    assert detail["logs"] and "gateway.log" in detail["logs"]
    assert detail["metrics"]["state"] in {"measured", "unattributed"}
    # A deployment-scoped log read is bounded to known names.
    log = app.get("/api/services/ticket-web/log", params={"name": "gateway.log"})
    assert log.status_code == 200 and "text" in log.json()
    assert app.get("/api/services/ticket-web/log", params={"name": "../config.toml"}).status_code == 400
    agents = app.get("/api/services/agents").json()
    assert any(item["id"] == agent["id"] for item in agents)


def test_service_actions_report_persisted_state_and_conflicts(client):
    app, deployment, stores, agent = client
    assert app.post("/api/services/ticket-web/probe").json()["state"] == "healthy"
    stopped = app.post("/api/services/ticket-web/stop", json={"reason": "web"}).json()
    assert stopped["accepted"] and stopped["deployment"]["desired_state"] == "stopped"
    assert app.get("/api/services/ticket-web").json()["observed_state"] == "stopped"
    started = app.post("/api/services/ticket-web/start").json()
    assert started["deployment"]["desired_state"] == "running" and started["deployment"]["generation"] == 2
    restarted = app.post("/api/services/ticket-web/restart", json={"reason": "web"}).json()
    assert restarted["deployment"]["generation"] == 3
    # Maintenance blocks restarts but is visible on the page.
    assert app.post("/api/services/ticket-web/maintenance", json={"enabled": True}).json()["maintenance"]
    row = next(item for item in app.get("/api/services").json()["services"]
               if item["deployment_id"] == "ticket-web")
    assert row["maintenance"] is True
    assert app.post("/api/services/ticket-web/restart", json={"reason": "web"}).status_code == 400
    app.post("/api/services/ticket-web/maintenance", json={"enabled": False})
    withdrawn = app.post("/api/services/ticket-web/withdraw", json={"reason": "web"}).json()
    assert withdrawn["publish_state"] == "withdrawn"
    # Withdrawn deployments keep running but cannot be re-published or bound.
    assert app.post("/api/services/ticket-web/publish").status_code == 400
    assert app.post("/api/services/ticket-web/bind", json={
        "agent_id": agent["id"], "alias": "tickets", "tools": ["ticket_search"]}).status_code == 400


def test_external_services_have_no_local_metrics_and_cannot_be_started(config, managed):
    catalog = ServiceCatalog(config)
    catalog.register_external(deployment_id="external-web", service_id="ticket-remote",
                              url="http://127.0.0.1:39872/mcp", contract="ticket-center/v1",
                              expected_tools=["ticket_search"], validate=False)
    with TestClient(create_app(config, background=False)) as app:
        row = next(item for item in app.get("/api/services").json()["services"]
                   if item["deployment_id"] == "external-web")
        assert row["mode"] == "external" and row["endpoint_kind"] == "external"
        assert row["metrics_state"] == "remote" and row["resources"] is None
        assert app.post("/api/services/external-web/start").status_code == 400
        assert app.post("/api/services/external-web/restart", json={"reason": "web"}).status_code == 400
        detail = app.get("/api/services/external-web").json()
        assert detail["mode"] == "external"


def test_from_checked_in_source_is_not_claimed_as_healthy(config, managed, service_repos):
    """A request accepted by the API must not be presented as an already-restored state."""
    app_client = TestClient(create_app(config, background=False))
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-accepted")
    with app_client as app:
        app.post("/api/services/ticket-accepted/stop", json={"reason": "web"})
        body = app.post("/api/services/ticket-accepted/start").json()
        assert body["accepted"] is True
        current = app.get("/api/services/ticket-accepted").json()
        assert current["desired_state"] == "running"
        assert current["health"]["gateway_alive"]
