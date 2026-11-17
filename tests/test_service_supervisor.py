"""002-C: deterministic supervision, recovery budgets, incidents and guardian."""
import json
import os
import time
from pathlib import Path

import pytest

from agent_base.guardian import GUARDIAN_INSTRUCTIONS, GuardianRunner, run_guardian_patrol
from agent_base.processes import alive, kill_tree
from agent_base.service_install import ManagedServices
from agent_base.service_supervisor import ServiceSupervisor, probe_deployment
from agent_base.service_store import ServiceStore
from test_gateway import DSH_BIN, gateway
from test_services_install import install_deployment


def crash_gateway(managed, deployment_id):
    deployment = managed.services.deployment(deployment_id)
    kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])


def test_probe_records_layers_and_closes_incident_after_recovery(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-supervised")
    supervisor = ServiceSupervisor(managed.config)
    first = supervisor.probe("ticket-supervised", business=True)
    assert first["state"] == "healthy"
    assert first["evidence"]["process"] and first["evidence"]["protocol"]
    assert first["evidence"]["business"] and first["evidence"]["business"]["ok"]
    assert not managed.services.incidents(deployment_id="ticket-supervised")

    crash_gateway(managed, "ticket-supervised")
    states = [supervisor.probe("ticket-supervised", business=False)["state"] for _ in range(3)]
    assert states == ["degraded", "degraded", "unhealthy"]
    incidents = managed.services.incidents(deployment_id="ticket-supervised")
    assert len(incidents) == 1 and incidents[0]["status"] == "open"
    # The declared restart_safe service was recovered automatically.
    recovered = managed.services.deployment("ticket-supervised")
    assert recovered["generation"] == 2 and recovered["recovery_attempts"] == 1
    assert managed.gateway_health(recovered)["gateway_alive"]

    followups = [supervisor.probe("ticket-supervised", business=False)["state"] for _ in range(3)]
    assert followups == ["degraded", "healthy", "healthy"]
    closed = managed.services.incidents(deployment_id="ticket-supervised")[0]
    assert closed["status"] == "closed" and closed["close_evidence"]


def test_recovery_budget_exhaustion_keeps_incident_open(managed, service_repos):
    install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-budget")
    supervisor = ServiceSupervisor(managed.config)
    generations = []
    for cycle in range(4):
        crash_gateway(managed, "ticket-budget")
        # Simulate the backoff window having elapsed (controllable clock).
        managed.services.update_deployment("ticket-budget", next_retry_at=0)
        for _ in range(3):
            supervisor.probe("ticket-budget", business=False)
        deployment = managed.services.deployment("ticket-budget")
        generations.append(deployment["generation"])
    # Three recoveries happened (generations 2,3,4); the fourth crash stays down.
    assert generations == [2, 3, 4, 4]
    final = managed.services.deployment("ticket-budget")
    assert final["recovery_attempts"] == 3
    assert not managed.gateway_health(final)["gateway_alive"]
    incidents = managed.services.incidents(deployment_id="ticket-budget")
    assert incidents[0]["status"] == "open"


def test_stopped_and_maintenance_states_are_respected(managed, service_repos):
    install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stopped")
    supervisor = ServiceSupervisor(managed.config)
    managed.stop("ticket-stopped", reason="fixture")
    result = supervisor.probe("ticket-stopped", business=False)
    assert result["state"] == "stopped"
    assert not managed.services.incidents(deployment_id="ticket-stopped")
    assert managed.services.deployment("ticket-stopped")["gateway_pid"] is None

    deployment = managed.start("ticket-stopped", reason="fixture")
    managed.services.update_deployment("ticket-stopped", maintenance=1)
    crash_gateway(managed, "ticket-stopped")
    for _ in range(3):
        supervisor.probe("ticket-stopped", business=False)
    # Maintenance blocks automatic revival; the incident records the fault.
    assert not managed.gateway_health(managed.services.deployment("ticket-stopped"))["gateway_alive"]
    assert managed.services.incidents(deployment_id="ticket-stopped")


def test_guardian_restart_policy_checked_server_side(managed, service_repos):
    install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-policy")
    with pytest.raises(ValueError, match="未授权看护重启"):
        managed.restart("ticket-policy", reason="fixture", actor="guardian")
    deployment = managed.services.deployment("ticket-policy")
    managed.services.update_deployment("ticket-policy",
                                       config={**deployment["config"], "allow_guardian_restart": True})
    restarted = managed.restart("ticket-policy", reason="fixture patrol", actor="guardian")
    assert restarted["generation"] == 2
    # Budget exhaustion also refuses guardian restarts.
    deployment = managed.services.deployment("ticket-policy")
    managed.services.update_deployment("ticket-policy", recovery_attempts=3,
                                       recovery_window_start=time.time(), next_retry_at=time.time() + 60)
    with pytest.raises(ValueError, match="恢复预算不足"):
        managed.restart("ticket-policy", reason="fixture", actor="guardian")


def test_supervisor_heartbeat_and_scheduler_integration(managed, service_repos, config):
    install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-heartbeat")
    supervisor = ServiceSupervisor(managed.config)
    supervisor.tick()
    state = ServiceSupervisor.supervisor_state(managed.config)
    assert state["state"] == "running" and state["last_tick"] > 0
    from agent_base.scheduler import Scheduler
    scheduler = Scheduler(config)
    result = scheduler.tick()
    assert "dispatched" in result
    assert ServiceSupervisor.supervisor_state(config)["state"] == "running"


def test_guardian_patrol_runs_through_real_entry(config, managed, service_repos, monkeypatch):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-guardian")
    crash_gateway(managed, "ticket-guardian")
    supervisor = ServiceSupervisor(managed.config)
    for _ in range(3):
        supervisor.probe("ticket-guardian", business=False)
    incident = managed.services.incidents(deployment_id="ticket-guardian")[0]

    # The patrol goes through the real runtime entry with the restricted MCP.
    monkeypatch.setattr("agent_base.guardian.GUARDIAN_INSTRUCTIONS",
                        "FIXTURE_GUARDIAN\nFIXTURE_GUARDIAN_DEPLOYMENT:ticket-guardian\n" + GUARDIAN_INSTRUCTIONS)
    result = run_guardian_patrol(config, trigger="test", deployment_id="ticket-guardian",
                                 incident_id=incident["id"])
    assert result.get("summary"), result
    # The fixture runtime works inside the guardian workspace.
    actions = json.loads((config.data / "guardian" / "workspace" / "captured-guardian.json").read_text())
    assert actions["probe_state"] in {"degraded", "unhealthy"}
    assert actions["overview_deployments"] >= 1
    incident = managed.services.incident(incident["id"])
    assert any(action.get("kind") == "guardian_note" for action in incident["actions"])
    # The guardian restart policy is enforced even from the restricted tool.
    from agent_base.service_install import ManagedServices
    with pytest.raises(ValueError, match="未授权看护重启"):
        ManagedServices(config).restart("ticket-guardian", reason="patrol", actor="guardian")


@pytest.mark.skipif(not DSH_BIN.is_file(), reason="本地 dsh 不可用")
def test_guardian_model_surface_is_only_restricted_operations(config, gateway, monkeypatch):
    """The model-visible tool catalog, not just the restricted server, is checked."""
    monkeypatch.setenv("ACCEPTANCE_GUARDIAN_KEY", "fake-guardian-credential")
    config.runtime.update(
        command=[str(DSH_BIN)], provider="acceptance-gateway", model="internal-flash", effort="high",
        providers={"acceptance-gateway": {"api": "openai-completions", "base_url": gateway.url,
                    "api_key_env": "ACCEPTANCE_GUARDIAN_KEY", "models": [{"id": "internal-flash",
                        "context_window": 131072, "reasoning_efforts": {"high": "high"}}]}})
    config.guardian["timeout_seconds"] = 45
    gateway.tool_call_response = lambda body: [
        gateway.chunk(body["model"], {"role": "assistant", "content": "只检查工具清单，未执行修复。"}),
        gateway.chunk(body["model"], {}, finish="stop")]
    result = GuardianRunner(config).run_once(trigger="surface-test")
    assert not result.get("error"), result
    names = [item["function"]["name"] for item in gateway.requests[0].get("tools", [])]
    unexpected = [name for name in names if not name.startswith("mcp__guardian__")]
    assert names and not unexpected, {"unexpected_model_tools": unexpected}
