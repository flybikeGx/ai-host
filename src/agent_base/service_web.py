"""Shared-service management API for the human administration page.

Every route reuses the catalog and deployment services (the same permission and
audit paths as the Admin MCP). A 200 response only means the request was
accepted: the page must show the persisted desired/observed state afterwards.
"""
from __future__ import annotations

import time

from fastapi import Body

from .config import Config
from .service_catalog import ServiceCatalog


def _guardian_state(config: Config) -> dict:
    from .guardian import guardian_status
    status = guardian_status(config)
    interval = config.guardian.get("interval_seconds", 300)
    last = status.get("last_patrol")
    if not status.get("enabled"):
        state = "disabled"
    elif not last:
        state = "never_run"
    elif time.time() - last.get("at", 0) > max(90, interval * 2):
        state = "stale"
    elif last.get("status") == "failed":
        state = "failed"
    else:
        state = "running"
    return {**status, "state": state, "interval_seconds": interval,
            "model_available": config.runtime.get("engine", "dsh") == "dsh"}


def _metrics(app, deployment_id: str) -> dict | None:
    dashboard = getattr(app.state, "dashboard", None)
    if dashboard is None:
        return None
    try:
        snapshot = dashboard.snapshot()
    except Exception:
        return None
    for row in snapshot.get("services", []) or []:
        if row["deployment_id"] == deployment_id:
            return {"state": row.get("metrics_state"), "resources": row.get("resources"),
                    "process_state": row.get("process_state")}
    return None


def _agent_names(catalog: ServiceCatalog) -> dict:
    return {item["id"]: item["name"] for item in catalog.services.agent_identities()}


def register(app, config: Config, api):
    catalog = ServiceCatalog(config)
    managed = catalog.managed
    stores = catalog.services

    def services_list():
        names = _agent_names(catalog)
        metrics = {}
        dashboard = getattr(app.state, "dashboard", None)
        if dashboard is not None:
            try:
                metrics = {row["deployment_id"]: row for row in
                           (dashboard.snapshot().get("services") or [])}
            except Exception:
                metrics = {}
        from .service_supervisor import ServiceSupervisor
        rows = []
        for deployment in stores.deployments():
            item = catalog.describe(deployment)
            row = metrics.get(deployment["id"], {})
            item.update(title=deployment["config"].get("title", ""),
                        endpoint_kind="builtin" if managed.is_builtin(deployment)
                        else "external" if deployment["mode"] == "external" else "managed",
                        observed_at=deployment["observed_at"],
                        process_state=row.get("process_state"),
                        metrics_state=row.get("metrics_state", "remote" if deployment["mode"] == "external" else "measured"),
                        resources=row.get("resources"),
                        bound_agents=[{"binding_id": b["binding_id"], "agent_id": b["agent_id"],
                                       "name": names.get(b["agent_id"], b["agent_id"]),
                                       "alias": b["alias"], "tools": b["tools"],
                                       "enabled": b["enabled"], "revision": b["revision"]}
                                      for b in item["bindings"]])
            rows.append(item)
        return {"services": rows, "supervisor": ServiceSupervisor.supervisor_state(config),
                "guardian": _guardian_state(config), "agents": list(names.items()),
                "sampled_at": time.time(),
                "limits": {"note": "外部服务只显示可观测状态，不挂本机进程指标"}}

    def service_detail(deployment_id: str):
        deployment = catalog._deployment(deployment_id)
        names = _agent_names(catalog)
        item = catalog.describe(deployment)
        bindings = []
        for binding in stores.bindings(deployment_id=deployment_id):
            bindings.append({**binding, "agent_name": names.get(binding["agent_id"], binding["agent_id"])})
        logs = []
        if managed.is_builtin(deployment):
            logs = ["service.log"]
        else:
            logs_dir = managed.deployment_root(deployment_id) / "logs"
            if logs_dir.is_dir():
                logs = sorted(path.name for path in logs_dir.iterdir() if path.is_file())[:20]
        return {**item, "title": deployment["config"].get("title", ""),
                "observed_at": deployment["observed_at"],
                "observed_evidence": deployment["observed_evidence"],
                "health": managed.gateway_health(deployment),
                "bindings": bindings,
                "incidents": stores.incidents(deployment_id=deployment_id),
                "health_events": stores.health_events(deployment_id=deployment_id, limit=20),
                "operations": stores.operations(deployment_id=deployment_id, limit=10),
                "logs": logs, "metrics": _metrics(app, deployment_id)}

    @app.get("/api/services", dependencies=api)
    def list_services():
        return services_list()

    @app.get("/api/services/agents", dependencies=api)
    def agents():
        return stores.agent_identities()

    @app.get("/api/services/{deployment_id}", dependencies=api)
    def detail(deployment_id: str):
        return service_detail(deployment_id)

    @app.post("/api/services/{deployment_id}/publish", dependencies=api)
    def publish(deployment_id: str):
        catalog._deployment(deployment_id)
        return catalog.publish(deployment_id)

    @app.post("/api/services/{deployment_id}/withdraw", dependencies=api)
    def withdraw(deployment_id: str, body: dict | None = Body(default=None)):
        return catalog.withdraw(deployment_id, reason=(body or {}).get("reason", "web-admin"))

    @app.post("/api/services/{deployment_id}/start", dependencies=api)
    def start(deployment_id: str):
        result = managed.start(deployment_id, reason="web-admin")
        return {"accepted": True, "deployment": result}

    @app.post("/api/services/{deployment_id}/stop", dependencies=api)
    def stop(deployment_id: str, body: dict | None = Body(default=None)):
        result = managed.stop(deployment_id, reason=(body or {}).get("reason", "web-admin"))
        return {"accepted": True, "deployment": result}

    @app.post("/api/services/{deployment_id}/restart", dependencies=api)
    def restart(deployment_id: str, body: dict | None = Body(default=None)):
        deployment = catalog._deployment(deployment_id)
        if deployment["maintenance"]:
            raise ValueError("部署维护中，不能重启；请先结束维护")
        result = managed.restart(deployment_id, reason=(body or {}).get("reason", "web-admin"))
        return {"accepted": True, "deployment": result}

    @app.post("/api/services/{deployment_id}/probe", dependencies=api)
    def probe(deployment_id: str):
        from .service_supervisor import probe_deployment
        return probe_deployment(config, deployment_id)

    @app.post("/api/services/{deployment_id}/maintenance", dependencies=api)
    def maintenance(deployment_id: str, body: dict = Body(...)):
        if "enabled" not in body:
            raise ValueError("需要 enabled")
        catalog._deployment(deployment_id)
        stores.update_deployment(deployment_id, maintenance=1 if body["enabled"] else 0)
        return {"accepted": True, "maintenance": bool(body["enabled"])}

    @app.post("/api/services/{deployment_id}/bind", dependencies=api)
    def bind(deployment_id: str, body: dict = Body(...)):
        agent_id = body.get("agent_id")
        if not agent_id and body.get("repo_source"):
            identity = stores.ensure_agent_identity(source=body["repo_source"],
                                                    name=body["repo_source"].removesuffix("repo:")[:40])
            agent_id = identity["id"]
        if not agent_id:
            raise ValueError("需要 agent_id 或 repo_source")
        tools = body.get("tools") or []
        if not isinstance(tools, list) or not tools:
            raise ValueError("需要工具数组")
        return catalog.bind(agent_id=agent_id, deployment_id=deployment_id,
                            alias=body.get("alias") or "service", tools=tools,
                            contract=body.get("contract"), scope=body.get("scope"),
                            expected_revision=body.get("expected_revision"))

    @app.post("/api/services/bindings/{binding_id}/disable", dependencies=api)
    def unbind(binding_id: str):
        return catalog.unbind(binding_id)

    @app.get("/api/services/{deployment_id}/log", dependencies=api)
    def log(deployment_id: str, name: str = "gateway.log", tail_bytes: int = 12000):
        return managed.read_log(deployment_id, name, tail_bytes=min(max(tail_bytes, 100), 65536))
