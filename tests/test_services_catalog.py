"""002-B: publication, external registration, bindings and unified resolution."""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.processes import alive
from agent_base.repositories import Repositories
from agent_base.review import Reviews
from agent_base.service import Service
from agent_base.service_catalog import ServiceCatalog
from agent_base.service_install import ManagedServices
from agent_base.service_resolver import ServiceResolver
from agent_base.service_store import ServiceStore
from test_services_install import exercise, expect_rejected, install_deployment


def v3_agent_repo(tmp_path, name, *, tools_note, required=True):
    """A v3 agent repo whose entrypoint drives the fake runtime fixture."""
    root = tmp_path / f"agent-{name}"
    if root.exists():
        return root
    root.mkdir()
    (root / "AGENTS.md").write_text(f"你是测试 Agent {name}；只通过共享服务完成工作，不编造工具结果。\n", encoding="utf-8")
    (root / "entrypoint.md").write_text(
        "FIXTURE_SERVICES\n"
        f"{tools_note}\n"
        "把结果写入 artifacts/services.json 并按统一汇报契约输出。\n", encoding="utf-8")
    (root / "agent.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "kind": "agent",
        "package": {"name": f"{name}-agent", "version": "0.1.0"},
        "services": [{"alias": "tickets", "service": "ticket-center", "contract": "ticket-center/v1",
                      "permissions": ["ticket_search"], "required": required}],
    }, allow_unicode=True), encoding="utf-8")
    (root / "completion").mkdir()
    (root / "artifacts").mkdir()
    (root / "agent.yaml").write_text((root / "agent.yaml").read_text() + "")
    # completion artifacts live in the manifest
    manifest = yaml.safe_load((root / "agent.yaml").read_text())
    manifest["completion"] = {"artifacts": [{"path": "artifacts/services.json", "min_bytes": 10}]}
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "v3 agent fixture"], check=True)
    return root


@pytest.fixture
def published_deployment(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-shared")
    catalog = ServiceCatalog(managed.config)
    return catalog.publish("ticket-shared")


def test_publish_requires_validation_and_catalog_lists(managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-unverified")
    catalog = ServiceCatalog(managed.config)
    # Simulate an unverified deployment (e.g. registered but never validated).
    managed.services.update_deployment("ticket-unverified", verification="not_verified")
    with pytest.raises(ValueError, match="尚未通过协议验证"):
        catalog.publish("ticket-unverified")
    managed.services.update_deployment("ticket-unverified", verification="protocol_verified")
    published = catalog.publish("ticket-unverified")
    assert published["publish_state"] == "published"
    listed = {item["deployment_id"]: item for item in catalog.list_services()}
    assert listed["ticket-unverified"]["publish_state"] == "published"
    assert listed["ticket-unverified"]["verification"] == "protocol_verified"
    withdrawn = catalog.withdraw("ticket-unverified", reason="fixture")
    assert withdrawn["publish_state"] == "withdrawn"
    with pytest.raises(ValueError, match="不能直接重新发布"):
        catalog.publish("ticket-unverified")


def test_two_agents_share_one_deployment_with_isolated_scopes(config, managed, service_repos,
                                                              published_deployment, tmp_path):
    repos = Repositories(config)
    catalog = ServiceCatalog(config)
    stores = ServiceStore(config.data)
    triage = repos.install(str(v3_agent_repo(tmp_path, "triage", tools_note="只允许查询")))
    ops = repos.install(str(v3_agent_repo(tmp_path, "ops", tools_note="查询并订阅")))
    triage_identity = stores.agent_identity_for_source("repo:" + triage["repo_id"])
    ops_identity = stores.agent_identity_for_source("repo:" + ops["repo_id"])
    assert triage_identity and ops_identity and triage_identity["id"] != ops_identity["id"]
    # The stable identity survives an installation version change.
    (tmp_path / "agent-triage" / "AGENTS.md").write_text(
        "你是测试 Agent triage（第二版）。\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path / "agent-triage"), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path / "agent-triage"), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=" + os.devnull,
                    "commit", "--quiet", "-m", "v2"], check=True)
    again = repos.install(str(tmp_path / "agent-triage"))
    assert again["id"] != triage["id"]
    assert stores.agent_identity_for_source("repo:" + again["repo_id"])["id"] == triage_identity["id"]

    catalog.bind(agent_id=triage_identity["id"], deployment_id="ticket-shared", alias="tickets",
                 tools=["ticket_search"])
    catalog.bind(agent_id=ops_identity["id"], deployment_id="ticket-shared", alias="tickets",
                 tools=["ticket_search", "ticket_subscribe"])

    service = Service(config)
    first = service.wait(service.submit(str(tmp_path / "agent-triage"))["id"], 40)
    assert first["status"] == "succeeded", first
    second = service.wait(service.submit(str(tmp_path / "agent-ops"))["id"], 40)
    assert second["status"] == "succeeded", second

    triage_result = json.loads(Path(first["result"]["artifacts"][0]["path"]).read_text())
    ops_result = json.loads(Path(second["result"]["artifacts"][0]["path"]).read_text())
    # One shared generation served both agents.
    assert triage_result["services"][0]["calls"][0]["result"]["boot_id"] == \
        ops_result["services"][0]["calls"][0]["result"]["boot_id"]
    # Scope isolation: the catalog and the actual calls agree.
    assert triage_result["services"][0]["tools"] == ["ticket_search"]
    assert ops_result["services"][0]["tools"] == ["ticket_search", "ticket_subscribe"]
    assert [c["tool"] for c in triage_result["services"][0]["calls"]] == ["ticket_search"]
    assert all(c["ok"] for c in ops_result["services"][0]["calls"])
    deployment = stores.deployment("ticket-shared")
    assert deployment["generation"] == 1

    # Agent lifecycle is decoupled: stopping one agent never stops the service.
    repos.stop(triage["id"])
    assert managed.gateway_health(stores.deployment("ticket-shared"))["gateway_alive"]
    third = service.wait(service.submit(str(tmp_path / "agent-ops"))["id"], 40)
    assert third["status"] == "succeeded", third


def test_revocation_rejects_immediately_and_required_missing_fails_run(config, managed, service_repos,
                                                                        published_deployment, tmp_path):
    repos = Repositories(config)
    catalog = ServiceCatalog(config)
    stores = ServiceStore(config.data)
    ops = repos.install(str(v3_agent_repo(tmp_path, "ops", tools_note="查询并订阅")))
    identity = stores.agent_identity_for_source("repo:" + ops["repo_id"])
    binding = catalog.bind(agent_id=identity["id"], deployment_id="ticket-shared", alias="tickets",
                           tools=["ticket_search", "ticket_subscribe"])
    service = Service(config)
    ok = service.wait(service.submit(str(tmp_path / "agent-ops"))["id"], 40)
    assert ok["status"] == "succeeded", ok

    # Unbind revokes existing grants at once.
    stores.save_binding(agent_id=identity["id"], deployment_id="ticket-shared", alias="tickets",
                        service_id="ticket-center", contract="ticket-center/v1",
                        tools=["ticket_search", "ticket_subscribe"], enabled=False)
    stores.revoke_grants(agent_id=identity["id"])
    deployment = stores.deployment("ticket-shared")
    token = stores.issue_grant({"kind": "call", "agent_id": identity["id"], "deployment_id": "ticket-shared",
                                "alias": "tickets", "tools": ["ticket_search"],
                                "pid": os.getpid(), "birth": _birth()}, 60)

    async def attempt():
        from test_services_install import exercise
        return await exercise(deployment["endpoint"], token, [("ticket_search", {"query": "x"})])

    with pytest.raises(BaseException):
        asyncio.run(attempt())

    # A required-but-unbound service fails the run before any business side effect.
    missing = service.wait(service.submit(str(tmp_path / "agent-ops"))["id"], 40)
    assert missing["status"] == "failed"
    assert "必需的共享服务" in missing["error"]


def test_contract_mismatch_and_unknown_tools_rejected(managed, service_repos, published_deployment):
    catalog = ServiceCatalog(managed.config)
    stores = ServiceStore(managed.config.data)
    agent = stores.ensure_agent_identity(source="repo:contract000000000", name="contract-agent")
    with pytest.raises(ValueError, match="契约不匹配"):
        catalog.bind(agent_id=agent["id"], deployment_id="ticket-shared", alias="tickets",
                     tools=["ticket_search"], contract="ticket-center/v2")
    with pytest.raises(ValueError, match="不在服务目录内"):
        catalog.bind(agent_id=agent["id"], deployment_id="ticket-shared", alias="tickets",
                     tools=["ticket_search", "not_a_tool"])


def test_external_registration_validates_real_protocol(config, managed, service_repos, monkeypatch):
    """External endpoints are reached through the platform access layer."""
    source = service_repos["ticket-center-http"]
    monkeypatch.setenv("TICKET_HTTP_AUTH", "Bearer external-secret")
    process = subprocess.Popen([os.sys.executable, str(source / "server.py"), "--host", "127.0.0.1",
                                "--port", "39871", "--data", str(source)],
                               env={**os.environ, "TICKET_HTTP_AUTH": "Bearer external-secret"},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import socket
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", 39871), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        catalog = ServiceCatalog(config)
        deployment = catalog.register_external(deployment_id="ticket-external", service_id="ticket-center",
                                               url="http://127.0.0.1:39871/mcp", contract="ticket-center/v1",
                                               expected_tools=["ticket_search"],
                                               headers={"Authorization": {"from_env": "TICKET_HTTP_AUTH"}},
                                               owner="fixture")
        assert deployment["mode"] == "external"
        assert deployment["verification"] == "protocol_verified"
        assert set(deployment["config"]["tools"]) >= {"ticket_search"}
        assert deployment["endpoint"].startswith("http://127.0.0.1:")
        catalog.publish("ticket-external")
        stores = ServiceStore(config.data)
        agent = stores.ensure_agent_identity(source="repo:external00000000", name="ext-agent")
        with pytest.raises(ValueError, match="不在服务目录内"):
            catalog.bind(agent_id=agent["id"], deployment_id="ticket-external", alias="tickets",
                         tools=["ticket_search", "not_a_real_tool"])
        binding = catalog.bind(agent_id=agent["id"], deployment_id="ticket-external", alias="tickets",
                               tools=["ticket_search"])
        job = managed.store.create({"repo": "fixture"})
        managed.store.update(job["id"], status="running", deadline=time.time() + 60)
        definition = {"kind": "call", "agent_id": agent["id"], "deployment_id": "ticket-external",
                      "alias": "tickets", "tools": ["ticket_search"], "scope": {},
                      "pid": os.getpid(), "birth": _birth()}
        token = stores.issue_grant(definition, 60)
        # The platform grants work through the local access layer; backend
        # credentials stay inside the gateway and never reach the client.
        answers = asyncio.run(exercise(deployment["endpoint"], token,
                                       [("ticket_search", {"query": "支付"})]))
        assert json.loads(answers[0].content[0].text)["auth_ok"] is True
        expect_rejected(deployment["endpoint"], token,
                        [("ticket_subscribe", {"agent": "a", "project": "p"})], "授权范围")
        # The resolver mounts the platform access layer with a platform grant,
        # not the external endpoint plus its service secret.
        env, patch = {}, config.data / "test-external.patch.yml"
        patch.write_text("[]\n", encoding="utf-8")
        instructions = ServiceResolver(config).attach(
            patch=patch, env=env, workspace=None, manifest={"services": [
                {"alias": "tickets", "service": "ticket-center", "contract": "ticket-center/v1",
                 "permissions": ["ticket_search"], "required": True}]},
            installation={"repo_id": "external00000000"}, run_id=job["id"], deadline=time.time() + 60)
        assert "tickets" in instructions
        from agent_base.engines import JsEnvironment, PatchLoader
        rows = yaml.load(patch.read_text(), Loader=PatchLoader)
        insert = next(p for row in rows for p in row.get("insert", [])
                      if p["id"] == "shared-service-tickets")
        assert insert["config"]["url"] == deployment["endpoint"]
        header_ref = insert["config"]["headers"]["Authorization"]
        assert isinstance(header_ref, JsEnvironment)
        assert env[header_ref.name].startswith("Bearer ")
        assert "external-secret" not in env[header_ref.name]
        # Unbinding revokes the platform grant at once; the remote process is
        # not touched by the platform.
        catalog.unbind(binding["id"])
        expect_rejected(deployment["endpoint"], token,
                        [("ticket_search", {"query": "支付"})], "401")
        with pytest.raises(ValueError, match="外部服务"):
            ManagedServices(config).restart("ticket-external", reason="fixture")
        assert alive(process.pid, None) or process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)


def _birth():
    from agent_base.processes import identity
    return identity(os.getpid())


def _scoped_call(config, managed, deployment, tools, scope):
    catalog = ServiceCatalog(config)
    if deployment["publish_state"] != "published":
        catalog.publish(deployment["id"])
    stores = ServiceStore(config.data)
    agent = stores.ensure_agent_identity(source="repo:" + "e" * 16, name="scope-agent")
    binding = catalog.bind(agent_id=agent["id"], deployment_id=deployment["id"], alias="tickets",
                           tools=tools, scope=scope)
    token = stores.issue_grant({"kind": "call", "agent_id": agent["id"],
                                "deployment_id": deployment["id"], "alias": "tickets",
                                "tools": tools, "scope": binding["scope"],
                                "pid": os.getpid(), "birth": _birth()}, 60)
    return deployment["endpoint"], token, binding, catalog


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_project_scope_is_enforced_server_side(config, managed, service_repos, transport):
    if transport == "http":
        deployment = install_deployment(
            managed, service_repos["ticket-center-http"], "scope-http",
            credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
            backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})[0]
    else:
        deployment = install_deployment(managed, service_repos["ticket-center-stdio"], "scope-stdio")[0]
    endpoint, token, binding, catalog = _scoped_call(
        config, managed, deployment, ["ticket_subscribe"], {"projects": ["allowed-project"]})
    assert binding["scope"] == {"projects": ["allowed-project"]}
    allowed = asyncio.run(exercise(endpoint, token, [
        ("ticket_subscribe", {"agent": "scope", "project": "allowed-project"})]))
    assert not allowed[0].isError
    try:
        answer = asyncio.run(exercise(endpoint, token, [
            ("ticket_subscribe", {"agent": "scope", "project": "forbidden-project"})]))[0]
        denied = answer.isError
    except BaseException as error:
        denied = "项目范围" in repr(error)
    assert denied, "forbidden project was accepted"
    with pytest.raises(ValueError, match="只支持 projects"):
        catalog.bind(agent_id=binding["agent_id"], deployment_id=deployment["id"], alias="other",
                     tools=["ticket_search"], scope={"namespaces": ["x"]})


def test_v3_service_declarations_are_exact(config, managed, service_repos):
    deployment, _ = install_deployment(managed, service_repos["ticket-center-stdio"], "ticket-exact")
    catalog = ServiceCatalog(config)
    catalog.publish("ticket-exact")
    stores = ServiceStore(config.data)
    agent = stores.ensure_agent_identity(source="repo:" + "f" * 16, name="explicit-agent")
    catalog.bind(agent_id=agent["id"], deployment_id="ticket-exact", alias="tickets",
                 tools=["ticket_search"])
    resolver = ServiceResolver(config)
    installation = {"repo_id": "f" * 16}
    empty = {"schema_version": 3, "kind": "agent", "services": []}
    assert resolver.resolve(manifest=empty, installation=installation) == []
    missing = {"schema_version": 3, "kind": "agent"}
    assert resolver.resolve(manifest=missing, installation=installation) == []
    declared = {"schema_version": 3, "kind": "agent", "services": [
        {"alias": "tickets", "service": "ticket-center", "contract": "ticket-center/v1",
         "permissions": ["ticket_search"], "required": True}]}
    resolved = resolver.resolve(manifest=declared, installation=installation)
    assert [entry["binding"]["alias"] for entry in resolved] == ["tickets"]
    # The Admin compatibility view may still list every enabled binding.
    admin = stores.ensure_agent_identity(source="admin", name="Admin", kind="builtin")
    catalog.bind(agent_id=admin["id"], deployment_id="ticket-exact", alias="tickets",
                 tools=["ticket_search"])
    assert resolver.resolve(manifest=None, installation=None, actor="admin")


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_project_scope_rejects_tools_without_project_parameter(config, managed, service_repos, transport):
    if transport == "http":
        deployment = install_deployment(
            managed, service_repos["ticket-center-http"], "scope-projectless-http",
            credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
            backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})[0]
    else:
        deployment = install_deployment(managed, service_repos["ticket-center-stdio"],
                                        "scope-projectless-stdio")[0]
    endpoint, token, _, _ = _scoped_call(
        config, managed, deployment, ["ticket_search"], {"projects": ["allowed-project"]})
    try:
        answer = asyncio.run(exercise(endpoint, token, [("ticket_search", {"query": "支付"})]))[0]
        denied = answer.isError
    except BaseException as error:
        denied = "项目范围" in repr(error) or "项目参数" in repr(error)
    assert denied, "projectless tool bypassed the project whitelist"


def _array_scope_repo(repo):
    source = repo / "server.py"
    text = source.read_text()
    text = text.replace('"status": {"type": "string", "enum": ["open", "closed"]}',
                        '"projects": {"type": "array", "items": {"type": "string"}}, '
                        '"status": {"type": "string", "enum": ["open", "closed"]}', 1)
    text = text.replace('        if arguments.get("status"):',
                        '        if arguments.get("projects"):\n'
                        '            results = [t for t in results if t.get("project") in arguments["projects"]]\n'
                        '        if arguments.get("status"):', 1)
    source.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "array scope"], check=True)
    return repo


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_empty_project_filter_cannot_read_outside_binding(config, managed, service_repos, transport):
    repo = _array_scope_repo(service_repos["ticket-center-" + transport])
    if transport == "http":
        deployment = install_deployment(
            managed, repo, "empty-scope-http",
            credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
            backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})[0]
    else:
        deployment = install_deployment(managed, repo, "empty-scope-stdio")[0]
    data = managed.deployment_root(deployment["id"]) / "data" / "tickets.jsonl"
    data.write_text("\n".join(json.dumps({"id": project, "title": "scope-list-marker",
                                           "project": project, "status": "open", "tags": []})
                              for project in ["allowed-project", "forbidden-project"]) + "\n")
    endpoint, token, _, _ = _scoped_call(config, managed, deployment, ["ticket_search"],
                                         {"projects": ["allowed-project"]})
    allowed = asyncio.run(exercise(endpoint, token, [
        ("ticket_search", {"query": "scope-list-marker", "projects": ["allowed-project"]})]))[0]
    assert [item["project"] for item in json.loads(allowed.content[0].text)["results"]] == ["allowed-project"]
    try:
        empty = asyncio.run(exercise(endpoint, token, [
            ("ticket_search", {"query": "scope-list-marker", "projects": []})]))[0]
        denied = empty.isError
    except BaseException as error:
        denied = "项目" in repr(error) or "范围" in repr(error)
    assert denied, "empty project filter bypassed the project whitelist"
