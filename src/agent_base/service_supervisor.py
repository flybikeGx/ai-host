"""Deterministic service supervision: probes, budgets, incidents, recovery.

The supervisor runs inside the host's resident scheduler loop (serve/worker),
needs no model, and is the only automatic recovery path for shared managed
deployments. Every mutation goes through the deployment lock so concurrent
Admin, guardian or serve processes cannot double-recover one deployment.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .config import Config
from .processes import identity
from .service_install import ManagedServices
from .service_store import ServiceStore

PROBE_LAYERS = ("process", "protocol", "business")


class SkipProbe(RuntimeError):
    """Another coordinator currently holds the deployment lock."""


class ServiceSupervisor:
    def __init__(self, config: Config):
        self.config = config
        self.services = ServiceStore(config.data)
        self.managed = ManagedServices(config)
        self.last_probe: dict[str, float] = {}

    # -- heartbeat ------------------------------------------------------------
    @property
    def heartbeat_file(self) -> Path:
        return self.config.data / "services" / "supervisor.json"

    def heartbeat(self, now: float):
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.heartbeat_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last_tick": now, "pid": os.getpid(),
                                         "birth": identity(os.getpid())}))
        temporary.replace(self.heartbeat_file)

    @classmethod
    def supervisor_state(cls, config: Config) -> dict:
        try:
            item = json.loads((config.data / "services" / "supervisor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "never_run"}
        state = "stale" if time.time() - item.get("last_tick", 0) > 90 else "running"
        return {**item, "state": state, "age_seconds": round(time.time() - item.get("last_tick", 0), 1)}

    # -- probing --------------------------------------------------------------
    def tick(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        results = []
        for deployment in self.services.deployments():
            if deployment["desired_state"] == "removed":
                continue
            interval = deployment["config"].get("health", {}).get("interval_seconds", 30)
            last = self.last_probe.get(deployment["id"], 0)
            if now - last < max(1, interval) * 0.9:
                continue
            self.last_probe[deployment["id"]] = now
            try:
                results.append(self.probe(deployment["id"], now, business=False))
            except SkipProbe:
                continue
            except (OSError, ValueError):
                continue
        self.heartbeat(now)
        self.managed.recover_operations()
        return results

    def probe(self, deployment_id: str, now: float | None = None, *, business: bool = True) -> dict:
        now = time.time() if now is None else now
        root = self.managed.deployment_root(deployment_id)
        root.mkdir(parents=True, exist_ok=True)
        # The lock guards observation bookkeeping only. Recovery runs after it
        # and is serialized by the deployment lock inside start/stop/restart,
        # whose healthy early-return prevents a second coordinator from
        # launching a duplicate generation.
        recovery = None
        with _try_lock(root / "deploy.lock"):
            deployment = self.services.deployment(deployment_id)
            if not deployment:
                raise ValueError(f"部署不存在：{deployment_id}")
            if deployment["desired_state"] == "removed":
                return {"deployment_id": deployment_id, "state": "removed", "changed": False}
            if deployment["mode"] == "external":
                return self._probe_external(deployment, now)
            if deployment["desired_state"] == "stopped":
                # An intentionally stopped deployment is not a fault.
                if deployment["observed_state"] != "stopped":
                    self.services.update_deployment(deployment["id"], observed_state="stopped",
                                                    observed_at=now,
                                                    observed_evidence='{"kind": "desired_stopped"}')
                return {"deployment_id": deployment["id"], "state": "stopped", "changed": False,
                        "evidence": {"checked_at": now, "desired": "stopped"}}
            result = self._probe_managed(deployment, now, business=business)
            recovery = result.pop("recovery")
        if recovery:
            self._attempt_recovery(self.services.deployment(deployment_id), now, **recovery)
        return result

    # -- managed --------------------------------------------------------------
    def _probe_managed(self, deployment: dict, now: float, *, business: bool) -> dict:
        if self.managed.is_builtin(deployment):
            # The knowledge component exposes MCP directly, not through a
            # deployment gateway: probe initialize/tool directory for the
            # protocol layer and the read-only catalog for the business layer.
            from .knowledge_migration import probe_builtin_layers
            layers = probe_builtin_layers(self.config, deployment, business=business)
            process_ok = bool(layers["process"])
            protocol_ok = bool(layers["protocol"])
            evidence = {"checked_at": now, "process": process_ok, "protocol": protocol_ok,
                        "generation": deployment["generation"], "in_flight": 0,
                        "builtin": "knowledge", "instance": layers.get("instance"),
                        "error": layers.get("error"),
                        "business": layers.get("business") if business else
                        {"ok": bool(layers.get("stats")), "health_stats": layers.get("stats")}}
        else:
            health = self.managed.gateway_health(deployment)
            gateway_alive = bool(health and health.get("gateway_alive"))
            process_ok = gateway_alive
            protocol_ok = bool(health and health.get("protocol_ok")) if gateway_alive else False
            evidence = {"checked_at": now, "process": gateway_alive,
                        "protocol": protocol_ok,
                        "generation": deployment["generation"],
                        "in_flight": (health or {}).get("in_flight", 0)}
            evidence["business"] = (self._business_probe(deployment)
                                    if business and process_ok and protocol_ok else None)
        changed = False
        recovery = None
        desired_running = deployment["desired_state"] == "running"

        if process_ok and protocol_ok:
            result = self._record_success(deployment, now, evidence)
            changed = result["changed"]
            state = result["state"]
        else:
            state, changed = self._record_failure(deployment, now, evidence,
                                                  process_ok=process_ok, desired_running=desired_running,
                                                  in_flight=evidence["in_flight"])
            if state == "unhealthy" and desired_running and not deployment["maintenance"]:
                recovery = {"process_ok": process_ok, "in_flight": evidence["in_flight"]}
        return {"deployment_id": deployment["id"], "state": state, "changed": changed,
                "evidence": evidence, "recovery": recovery}

    def _record_success(self, deployment: dict, now: float, evidence: dict) -> dict:
        failures = deployment["probe_failures"]
        successes = deployment["probe_successes"] + 1
        threshold = deployment["config"].get("health", {}).get("recovery_threshold", 2)
        previous = deployment["observed_state"]
        state = "healthy"
        if previous in {"unhealthy", "degraded", "starting"} and successes < threshold:
            state = "degraded"
        changed = state != previous
        self.services.update_deployment(deployment["id"], probe_failures=0, probe_successes=successes,
                                        observed_state=state, observed_at=now,
                                        observed_evidence=json.dumps(evidence, ensure_ascii=False))
        self.services.health_event(deployment["id"], "protocol", state, evidence)
        if state == "healthy":
            self._close_incidents(deployment, now, evidence)
        # The recovery budget intentionally survives short healthy spells; it
        # only resets when the whole window has passed (crash-loop protection).
        return {"state": state, "changed": changed}

    def _record_failure(self, deployment: dict, now: float, evidence: dict, *, process_ok: bool,
                        desired_running: bool, in_flight: int) -> tuple[str, bool]:
        failures = deployment["probe_failures"] + 1
        threshold = deployment["config"].get("health", {}).get("failure_threshold", 3)
        previous = deployment["observed_state"]
        # Startup grace: a recently started generation is 'starting', not failed.
        started_at = None
        runtime = self.managed._runtime_state(deployment)
        if runtime and runtime.get("started_at"):
            started_at = runtime["started_at"]
        elif previous == "starting" and deployment.get("observed_at"):
            # Builtin components have no gateway runtime file; the observed
            # transition time is the same generation-start evidence.
            started_at = deployment["observed_at"]
        grace = deployment["config"].get("health", {}).get("startup_timeout_seconds", 60)
        if previous == "starting" and started_at and now - started_at < grace and process_ok:
            self.services.update_deployment(deployment["id"], probe_failures=failures,
                                            observed_state="starting", observed_at=now,
                                            observed_evidence=json.dumps(evidence, ensure_ascii=False))
            return "starting", False
        state = "unhealthy" if failures >= threshold else "degraded"
        changed = state != previous
        self.services.update_deployment(deployment["id"], probe_failures=failures,
                                        probe_successes=0, observed_state=state, observed_at=now,
                                        observed_evidence=json.dumps(evidence, ensure_ascii=False))
        self.services.health_event(deployment["id"], "process" if not process_ok else "protocol", state, evidence)
        if state == "unhealthy":
            self._open_incident(deployment, now, evidence, process_ok=process_ok)
        return state, changed

    def _open_incident(self, deployment: dict, now: float, evidence: dict, *, process_ok: bool):
        agents = [b["agent_id"] for b in self.services.bindings(deployment_id=deployment["id"]) if b["enabled"]]
        summary = ("服务进程退出" if not process_ok else "服务协议探测连续失败")
        incident = self.services.open_incident(deployment["id"], deployment["generation"], summary, agents)
        self.services.record_incident_action(incident["id"], {"kind": "probe_failure", "evidence": {
            "process": evidence.get("process"), "protocol": evidence.get("protocol"),
            "probe_failures": deployment["probe_failures"] + 1}})
        self._notify_guardian(deployment, incident)
        return incident

    def _close_incidents(self, deployment: dict, now: float, evidence: dict):
        for incident in self.services.incidents(deployment_id=deployment["id"]):
            if incident["status"] == "closed":
                continue
            if incident["status"] == "open":
                self.services.update_incident(incident["id"], status="monitoring")
                continue
            # monitoring: sustained health closes it with evidence.
            self.services.update_incident(incident["id"], status="closed",
                                          close_evidence=json.dumps(
                                              {"verified_at": now, "observed_state": "healthy",
                                               "evidence": evidence}, ensure_ascii=False))

    def _attempt_recovery(self, deployment: dict, now: float, *, process_ok: bool, in_flight: int):
        budget = self.managed.recovery_budget(deployment, now=now)
        if not budget["allowed"]:
            self.services.health_event(deployment["id"], "process", "recovery_budget_exhausted",
                                       {"reason": budget["reason"], "attempts": budget.get("attempts")})
            return
        if process_ok:
            # Alive but unhealthy: restart only when it is declared safe and
            # nothing is in flight; otherwise escalate, never force-kill.
            revision = self.services.revision(deployment["revision_id"]) if deployment.get("revision_id") else None
            manifest = revision["manifest"] if revision else {}
            if not manifest.get("operations", {}).get("restart_safe"):
                self.services.health_event(deployment["id"], "process", "recovery_skipped",
                                           {"reason": "未声明 restart_safe"})
                return
            if in_flight:
                self.services.health_event(deployment["id"], "process", "recovery_skipped",
                                           {"reason": "存在在途业务调用，不能安全排空", "in_flight": in_flight})
                return
        self.managed.consume_recovery(deployment["id"], budget["next_retry_at"])
        try:
            if process_ok:
                self.managed.stop(deployment["id"], reason="supervisor-recovery", drain=False)
            self.managed.start(deployment["id"], reason="supervisor-recovery")
            self.services.health_event(deployment["id"], "process", "recovery_started",
                                       {"generation": self.services.deployment(deployment["id"])["generation"]})
        except (OSError, ValueError) as error:
            self.services.health_event(deployment["id"], "process", "recovery_failed",
                                       {"error": str(error)[:300]})

    # -- external -------------------------------------------------------------
    def _probe_external(self, deployment: dict, now: float) -> dict:
        from .service_catalog import ServiceCatalog
        catalog = ServiceCatalog(self.config)
        result = catalog.validate_external(deployment["id"])
        evidence = {"checked_at": now, "protocol": result["ok"], "errors": result["errors"]}
        state = "healthy" if result["ok"] else "unhealthy"
        previous = deployment["observed_state"]
        self.services.health_event(deployment["id"], "protocol", state, evidence)
        if state == "unhealthy" and previous != "unhealthy":
            agents = [b["agent_id"] for b in self.services.bindings(deployment_id=deployment["id"]) if b["enabled"]]
            incident = self.services.open_incident(deployment["id"], None, "外部服务探测失败", agents)
            self.services.record_incident_action(incident["id"], {"kind": "probe_failure",
                                                                  "evidence": result["errors"]})
            self._notify_guardian(deployment, incident)
        elif state == "healthy" and previous == "unhealthy":
            self._close_incidents(deployment, now, evidence)
        return {"deployment_id": deployment["id"], "state": state, "changed": state != previous,
                "evidence": evidence}

    # -- business probe -------------------------------------------------------
    def _business_probe(self, deployment: dict) -> dict | None:
        revision = self.services.revision(deployment["revision_id"]) if deployment.get("revision_id") else None
        if not revision:
            return None
        probe = revision["manifest"].get("health", {}).get("probe_tool")
        if not probe:
            return None
        token = self.managed.issue_probe_grant(deployment["id"], 60)
        return asyncio.run(self._call_probe(deployment["endpoint"], probe, token))

    @staticmethod
    async def _call_probe(endpoint: str, probe: dict, token: str) -> dict:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    async with asyncio.timeout(15):
                        await session.initialize()
                        answer = await session.call_tool(probe["tool"], probe.get("arguments", {}))
        return {"ok": not answer.isError, "result": answer.content[0].text[:200] if answer.content else None}

    # -- guardian hook --------------------------------------------------------
    def _notify_guardian(self, deployment: dict, incident: dict):
        """Event-triggered patrol; the deterministic probes continue regardless."""
        if not self.config.guardian.get("enabled"):
            return
        try:
            from .guardian import trigger_guardian
            trigger_guardian(self.config, deployment["id"], incident["id"])
        except (OSError, ValueError, ImportError):
            pass


def probe_deployment(config: Config, deployment_id: str, *, business: bool = True) -> dict:
    """Explicit three-layer probe used by Admin and guardian tools."""
    supervisor = ServiceSupervisor(config)
    return supervisor.probe(deployment_id, business=business)


class _try_lock:
    """Non-blocking deployment lock; skip when another coordinator holds it."""

    def __init__(self, path: Path, timeout: float = 0.2):
        self.path = path
        self.timeout = timeout
        self.held = False
        self.stream = None

    def __enter__(self):
        import os as _os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if _os.name == "nt":
                import msvcrt
                self.stream.write(b"0")
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError as error:
            self.stream.close()
            raise SkipProbe("部署锁被其他协调者持有") from error
        return self

    def __exit__(self, *exc):
        if self.held:
            import os as _os
            if _os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        if self.stream:
            self.stream.close()
        return False
