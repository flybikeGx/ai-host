"""002-A: v3 manifests, install control plane and deployment gateways."""
import asyncio
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.manifests import load_service_manifest
from agent_base.package import load_package
from agent_base.processes import alive
from agent_base.review import Reviews
from agent_base.service_catalog import ServiceCatalog
from agent_base.service_install import ManagedServices
from agent_base.service_store import ServiceStore


def install_deployment(managed, repo, deployment_id, **kwargs):
    reviews = Reviews(managed.config)
    candidate = reviews.prepare(str(repo), None)
    plan = managed.plan_install(candidate, deployment_id=deployment_id, owner="fixture", **kwargs)
    operation = managed.apply_install(plan, background=False)
    assert operation["status"] == "succeeded", operation
    return managed.services.deployment(deployment_id), operation


def call_grant(config, deployment, tools, *, actor="repo:aabbccddeeff0011", run=None):
    store = ServiceStore(config.data)
    agent = store.ensure_agent_identity(source=actor, name="agent-fixture")
    store.save_binding(agent_id=agent["id"], deployment_id=deployment["id"], alias="tickets",
                       service_id=deployment["service_id"], contract="ticket-center/v1", tools=tools)
    definition = {"kind": "call", "agent_id": agent["id"], "deployment_id": deployment["id"],
                  "alias": "tickets", "tools": tools, "pid": os.getpid(), "birth": _birth()}
    if run:
        definition.update(run_id=run)
    return store.issue_grant(definition, 60)


def _birth():
    from agent_base.processes import identity
    return identity(os.getpid())


async def exercise(endpoint, token, calls):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
        async with streamable_http_client(endpoint, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return [(await session.call_tool(name, arguments)) for name, arguments in calls]


def expect_rejected(endpoint, token, calls, marker):
    """The SDK wraps MCP errors in an ExceptionGroup on session close."""
    try:
        asyncio.run(exercise(endpoint, token, calls))
    except BaseException as error:
        assert marker in repr(error), repr(error)
    else:
        raise AssertionError(f"expected rejection {marker!r} was not raised")


def test_v3_manifests_parse_and_reject_unknown_fields(service_repos, tmp_path):
    manifest = load_service_manifest(service_repos["ticket-center-stdio"])
    assert manifest["kind"] == "mcp-service"
    assert manifest["service"]["id"] == "ticket-center"
    assert manifest["runtime"]["transport"] == "stdio"
    assert manifest["health"]["expected_tools"] == ["ticket_search", "ticket_subscribe"]
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "Installation.md").write_text(
        "# x\n\n```agent-base-install\nschema_version: 3\nkind: mcp-service\n"
        "service: {id: bad-service, contract: ticket-center/v1}\n"
        "runtime: {transport: stdio, argv: [\"${runtime.python}\", \"s.py\"]}\n"
        "bogus_section: {}\n```\n")
    with pytest.raises(ValueError, match="未知服务清单字段"):
        load_service_manifest(broken)
    agent = load_package(service_repos["triage-agent"])
    assert agent["schema_version"] == 3 and agent["kind"] == "agent"
    assert agent["services"][0]["alias"] == "tickets"
    (service_repos["triage-agent"] / "agent.yaml").write_text(
        "schema_version: 3\nkind: agent\nmcp: {servers: []}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="v3 agent 不再声明 repo 私有 mcp"):
        load_package(service_repos["triage-agent"])


def test_stdio_install_validates_business_probe_and_shares_revisions(managed, service_repos):
    deployment, operation = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    assert deployment["desired_state"] == "running"
    assert deployment["publish_state"] == "unpublished"
    assert deployment["verification"] == "business_verified"
    assert operation["plan"]["result"]["tools"] == ["ticket_search", "ticket_subscribe"]
    assert deployment["generation"] == 1
    health = managed.gateway_health(deployment)
    assert health["gateway_alive"] and health["ready"] and health["protocol_ok"]
    assert health["backend"]["pid"] and alive(health["backend"]["pid"], health["backend"]["birth"])
    # Re-install with the same pinned candidate reuses the immutable revision.
    again, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    assert again["generation"] == 1 and again["gateway_pid"] == deployment["gateway_pid"]


def test_http_install_routes_private_port_and_rejects_scope(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-http"], "ticket-http",
                                       credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
                                       backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})
    assert deployment["verification"] == "business_verified"
    health = managed.gateway_health(deployment)
    assert health["backend"]["port"] and health["protocol_ok"]
    token = call_grant(managed.config, deployment, ["ticket_search"])
    answers = asyncio.run(exercise(deployment["endpoint"], token, [
        ("ticket_search", {"query": "支付"})]))
    assert json.loads(answers[0].content[0].text)["total"] == 1
    # The advertised tool directory matches the enforced call scope.
    async def listed():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(deployment["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return [tool.name for tool in (await session.list_tools()).tools]
    assert asyncio.run(listed()) == ["ticket_search"]
    expect_rejected(deployment["endpoint"], token,
                    [("ticket_subscribe", {"agent": "a", "project": "p"})], "授权范围")


def test_gateway_enforces_grants_and_run_liveness(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    token = call_grant(managed.config, deployment, ["ticket_search"])
    # No credential at all: plain HTTP is refused before any MCP traffic.
    async def reachable():
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                response = await client.post(deployment["endpoint"], content=b"{}",
                                             headers={"Accept": "application/json"})
                return response.status_code
        except httpx.HTTPError:
            return None
    assert asyncio.run(reachable()) == 401
    # A finished run's grant is refused immediately (revocation semantics).
    job = managed.store.create({"repo": "fixture"})
    managed.store.update(job["id"], status="running", deadline=time.time() + 60)
    token = call_grant(managed.config, deployment, ["ticket_search"], run=job["id"])
    answers = asyncio.run(exercise(deployment["endpoint"], token, [("ticket_search", {"query": "x"})]))
    assert json.loads(answers[0].content[0].text)["ok"] is True
    managed.store.update(job["id"], status="succeeded")
    # The guard refuses the whole request (HTTP 403) once the owning run ended.
    expect_rejected(deployment["endpoint"], token, [("ticket_search", {"query": "x"})], "403")


def test_missing_credentials_block_plan_and_leak_nothing(managed, service_repos, monkeypatch):
    monkeypatch.delenv("TICKET_SERVICE_AUTH", raising=False)
    reviews = Reviews(managed.config)
    candidate = reviews.prepare(str(service_repos["ticket-center-stdio"]), None)
    plan = managed.plan_install(candidate, deployment_id="ticket-stdio")
    assert "TICKET_SERVICE_AUTH" in plan["missing"]
    with pytest.raises(ValueError, match="缺少环境配置"):
        managed.apply_install(plan, background=False)


def test_install_step_failure_needs_input_and_keeps_data(managed, service_repos):
    reviews = Reviews(managed.config)
    candidate = reviews.prepare(str(service_repos["ticket-center-stdio"]), None)
    plan = managed.plan_install(candidate, deployment_id="ticket-broken")
    plan["candidate_id"] = candidate["id"]
    # Sabotage the frozen manifest after planning: the step must fail loudly.
    source = Path(candidate["root"]) / "checkout"
    (source / "prepare.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    operation = managed.apply_install(plan, background=False)
    assert operation["status"] == "needs_input"
    assert "不会自动重跑" in operation["error"]
    deployment = managed.services.deployment("ticket-broken")
    assert deployment["gateway_pid"] is None  # no orphan instance
    assert (managed.deployment_root("ticket-broken") / "data").is_dir()


def test_crashed_executor_requires_input_for_unknown_step(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    operation = managed.services.create_operation(deployment_id="ticket-crash", kind="install",
                                                  plan={"candidate_id": "x", "service_id": "ticket-center"})
    managed.services.update_operation(operation["id"], status="installing",
                                       plan={"progress": {"in_flight_step": 0}})
    # Simulate a dead executor (pid that cannot exist).
    managed.services.update_operation(operation["id"], pid=99999999, birth=1.0)
    managed.recover_operations()
    recovered = managed.services.operation(operation["id"])
    assert recovered["status"] == "needs_input"
    assert "结果未知" in recovered["error"]


def test_stop_start_new_generation_and_no_leftovers(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    first_pid = deployment["gateway_pid"]
    stopped = managed.stop("ticket-stdio", reason="fixture")
    assert stopped["observed_state"] == "stopped" and stopped["desired_state"] == "stopped"
    assert not alive(first_pid, deployment["gateway_birth"])
    started = managed.start("ticket-stdio", reason="fixture")
    assert started["generation"] == 2 and started["gateway_pid"] != first_pid
    assert managed.gateway_health(started)["gateway_alive"]


def test_concurrent_installs_produce_one_active_generation(managed, service_repos):
    reviews = Reviews(managed.config)
    candidate = reviews.prepare(str(service_repos["ticket-center-stdio"]), None)
    plan = managed.plan_install(candidate, deployment_id="ticket-race")
    with ThreadPoolExecutor(max_workers=2) as pool:
        operations = list(pool.map(lambda _: managed.apply_install(plan, background=False,
                                                                   idempotency_key="race"), range(2)))
    # One durable operation; the loser of the race sees the duplicate flag and
    # a snapshot of the in-flight status, never a second execution.
    assert operations[0]["id"] == operations[1]["id"]
    assert sum(bool(op.get("duplicate")) for op in operations) == 1
    final = managed.get_operation(operations[0]["id"], wait=5)
    assert final["status"] == "succeeded"
    deployment = managed.services.deployment("ticket-race")
    assert deployment["generation"] == 1


def test_gateway_environment_keeps_only_declared_secrets(managed, service_repos, monkeypatch):
    monkeypatch.setenv("SOME_OTHER_SECRET", "leak-me")
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    env, missing = managed.gateway_environment(deployment)
    assert not missing
    assert "TICKET_SERVICE_AUTH" in env
    assert "SOME_OTHER_SECRET" not in env
    from agent_base.engines import PROVIDER_CREDENTIAL_ALIAS
    assert PROVIDER_CREDENTIAL_ALIAS not in env


def test_read_log_is_tail_and_redacts_bearer_tokens(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-stdio")
    logs = managed.deployment_root("ticket-stdio") / "logs"
    (logs / "gateway.log").open("a").write("Authorization: Bearer abcdefghijklmnop123456\nline2\n")
    result = managed.read_log("ticket-stdio", "gateway.log")
    assert "Bearer <redacted>" in result["text"] and "abcdefghijklmnop123456" not in result["text"]
    with pytest.raises(ValueError):
        managed.read_log("ticket-stdio", "../config.toml")


def test_stdio_gateway_preserves_schema_and_error_flag(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-contract")
    token = managed.issue_probe_grant("ticket-contract")

    async def inspect():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(deployment["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    missing = await session.call_tool("ticket_search", {})
                    return listing, missing
    listing, missing = asyncio.run(inspect())
    tool = next(item for item in listing.tools if item.name == "ticket_search")
    assert tool.inputSchema.get("required") == ["query"]
    assert "query" in tool.inputSchema.get("properties", {})
    assert missing.isError


def test_http_listing_does_not_leak_in_flight(managed, service_repos):
    deployment, _ = install_deployment(
        managed, service_repos["ticket-center-http"], "ticket-flight",
        credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
        backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})
    token = call_grant(managed.config, deployment, ["ticket_search"])
    asyncio.run(exercise(deployment["endpoint"], token, [("ticket_search", {"query": "支付"})]))
    health = managed.gateway_health(managed.services.deployment("ticket-flight"))
    assert health["in_flight"] == 0


def test_reinstall_switches_to_new_revision_backend(managed, service_repos):
    repo = service_repos["ticket-center-stdio"]
    first, _ = install_deployment(managed, repo, "ticket-upgrade")
    source = repo / "server.py"
    source.write_text(source.read_text().replace(
        '"ok": True, "results": results[:20]',
        '"implementation_version": "new", "ok": True, "results": results[:20]'))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "new backend"], check=True)
    updated, operation = install_deployment(managed, repo, "ticket-upgrade")
    assert operation["status"] == "succeeded"
    assert first["revision_id"] != updated["revision_id"]
    assert updated["generation"] > first["generation"]
    token = managed.issue_probe_grant("ticket-upgrade")
    answer = asyncio.run(exercise(updated["endpoint"], token, [("ticket_search", {"query": "支付"})]))[0]
    assert json.loads(answer.content[0].text)["implementation_version"] == "new"


def test_failed_upgrade_reports_failed_and_leaves_no_active_gateway(managed, service_repos):
    repo = service_repos["ticket-center-stdio"]
    first, _ = install_deployment(managed, repo, "ticket-bad-upgrade")
    source = repo / "server.py"
    source.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "broken backend"], check=True)
    reviews = Reviews(managed.config)
    plan = managed.plan_install(reviews.prepare(str(repo), None), deployment_id="ticket-bad-upgrade")
    operation = managed.apply_install(plan, background=False)
    assert operation["status"] != "succeeded"
    current = managed.services.deployment("ticket-bad-upgrade")
    assert not alive(current["gateway_pid"], current["gateway_birth"])
    assert current["desired_state"] in {"stopped", "running"}  # no orphan process is the invariant
    assert first["generation"] < current["generation"]


def test_busy_upgrade_keeps_old_generation_and_inflight_call(managed, service_repos, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    repo = service_repos["ticket-center-stdio"]
    source = repo / "server.py"
    source.write_text(source.read_text().replace(
        '        query = arguments["query"].lower()',
        '        if arguments["query"] == "slow-busy":\n            await asyncio.sleep(2)\n'
        '        query = arguments["query"].lower()'))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "slow backend"], check=True)
    first, _ = install_deployment(managed, repo, "ticket-busy")
    catalog = ServiceCatalog(managed.config)
    catalog.publish("ticket-busy")
    stores = ServiceStore(managed.config.data)
    agent = stores.ensure_agent_identity(source="repo:" + "b" * 16, name="busy-agent")
    catalog.bind(agent_id=agent["id"], deployment_id="ticket-busy", alias="tickets",
                 tools=["ticket_search"])
    token = stores.issue_grant({"kind": "call", "agent_id": agent["id"], "deployment_id": "ticket-busy",
                                "alias": "tickets", "tools": ["ticket_search"], "scope": {},
                                "pid": os.getpid(), "birth": _birth()}, 120)
    source.write_text(source.read_text().replace(
        '"ok": True, "results": results[:20]',
        '"implementation_version": "new", "ok": True, "results": results[:20]'))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "new backend"], check=True)
    plan = managed.plan_install(Reviews(managed.config).prepare(str(repo), None),
                                deployment_id="ticket-busy")
    monkeypatch.setattr("agent_base.service_install.DRAIN_TIMEOUT", 0.15)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, exercise(
            first["endpoint"], token, [("ticket_search", {"query": "slow-busy"})]))
        until = time.time() + 10
        while time.time() < until:
            if (managed.gateway_health(first) or {}).get("in_flight"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("slow call did not enter the gateway")
        operation = managed.apply_install(plan, background=False)
        current = managed.services.deployment("ticket-busy")
        health = managed.gateway_health(current) or {}
        answers = future.result(timeout=10)
    assert operation["status"] != "succeeded"
    assert current["revision_id"] == first["revision_id"]
    assert current["desired_state"] == "running"
    assert health.get("gateway_alive") and health.get("ready")
    assert answers[0].isError is False


def test_pre_switch_failure_keeps_existing_generation(managed, service_repos):
    repo = service_repos["ticket-center-stdio"]
    first, _ = install_deployment(managed, repo, "ticket-prefail")
    prepare = repo / "prepare.py"
    prepare.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "broken prepare"], check=True)
    plan = managed.plan_install(Reviews(managed.config).prepare(str(repo), None),
                                deployment_id="ticket-prefail")
    operation = managed.apply_install(plan, background=False)
    assert operation["status"] == "needs_input"
    current = managed.services.deployment("ticket-prefail")
    health = managed.gateway_health(current) or {}
    assert current["revision_id"] == first["revision_id"]
    assert current["desired_state"] == "running"
    assert health.get("gateway_alive") and health.get("ready")


def test_concurrent_start_and_restart_cannot_activate_install_candidate(managed, service_repos):
    repo = service_repos["ticket-center-stdio"]
    first, _ = install_deployment(managed, repo, "install-gate")
    source = repo / "server.py"
    source.write_text(source.read_text().replace(
        '"ok": True, "results": results[:20]',
        '"implementation_version": "candidate", "ok": True, "results": results[:20]'))
    prepare = repo / "prepare.py"
    prepare.write_text('import sys,time\nfrom pathlib import Path\n'
                       'data=Path(sys.argv[1])\n(data/"install-entered").touch()\n'
                       'until=time.monotonic()+20\n'
                       'while not (data/"release-install").exists() and time.monotonic()<until: time.sleep(.02)\n'
                       'sys.exit(3)\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "candidate install"], check=True)
    plan = managed.plan_install(Reviews(managed.config).prepare(str(repo), None),
                                deployment_id="install-gate")
    data = managed.deployment_root("install-gate") / "data"
    operation = managed.apply_install(plan, background=True)
    try:
        until = time.time() + 10
        while not (data / "install-entered").exists() and time.time() < until:
            time.sleep(0.02)
        assert (data / "install-entered").exists()
        assert managed.get_operation(operation["id"])["status"] == "installing"
        with pytest.raises(ValueError, match="正在安装"):
            managed.start("install-gate", reason="concurrent-start")
        with pytest.raises(ValueError, match="正在安装"):
            managed.restart("install-gate", reason="concurrent-restart")
        current = managed.services.deployment("install-gate")
        assert current["revision_id"] == first["revision_id"]
    finally:
        (data / "release-install").touch()
        finished = managed.get_operation(operation["id"], wait=15)
    assert finished["status"] == "needs_input"
    current = managed.services.deployment("install-gate")
    health = managed.gateway_health(current) or {}
    assert current["revision_id"] == first["revision_id"]
    assert health.get("revision_id") == first["revision_id"]
    assert health.get("gateway_alive")
