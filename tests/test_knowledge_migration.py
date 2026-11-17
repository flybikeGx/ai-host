"""002-D1: isolated knowledge takeover, explicit bindings and data verification."""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.engines import PatchLoader
from agent_base.knowledge import Knowledge
from agent_base.knowledge_migration import (apply, builtin_grant, database_state, preflight,
                                            rollback, summary, verify)
from agent_base.knowledge_service import KnowledgeService
from agent_base.service_catalog import ServiceCatalog
from agent_base.service_install import ManagedServices
from agent_base.service_resolver import ServiceResolver
from agent_base.service_store import ServiceStore

REPO_ACTOR = "repo:" + "a" * 16


def seed_knowledge(config):
    """One project, one published version, one pending submission and two legacy actors."""
    store = Knowledge(config)
    namespace = store.save_namespace({"slug": "project-a", "name": "项目 A", "description": "隔离验收"})
    store.bind("admin", {"primary_namespace_id": namespace["id"],
                         "read_namespace_ids": [namespace["id"]],
                         "submit_namespace_ids": [namespace["id"]]})
    document = store.publish(namespace["id"], {"title": "发布规则", "summary": "摘要",
                                               "body_markdown": "# 日切\n每日零点结算。", "tags": ["规则"]})
    submission = store.submit(store.binding("admin"), namespace["id"], "create",
                              {"title": "待审规则", "summary": "待审", "body_markdown": "# 草稿\n内容"},
                              "隔离迁移验收", "seed-submission")
    identities = ServiceStore(config.data)
    identities.ensure_agent_identity(source=REPO_ACTOR, name="triage-agent")
    store.bind(REPO_ACTOR, {"primary_namespace_id": namespace["id"],
                            "read_namespace_ids": [namespace["id"]],
                            "submit_namespace_ids": [namespace["id"]]})
    return {"namespace": namespace, "document": document, "submission": submission}


def agent_token(config, agent_id):
    stores = ServiceStore(config.data)
    binding = next(item for item in stores.bindings(agent_id=agent_id, deployment_id="project-knowledge")
                   if item["enabled"])
    deployment = stores.deployment("project-knowledge")
    return builtin_grant(config, deployment, binding, deadline=time.time() + 120), deployment


async def mcp_calls(endpoint, token, calls):
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
        async with streamable_http_client(endpoint, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                return [await session.call_tool(name, arguments) for name, arguments in calls]


def call(endpoint, token, calls):
    return asyncio.run(mcp_calls(endpoint, token, calls))


def test_preflight_is_read_only_and_freezes_source(managed):
    config = managed.config
    seeded = seed_knowledge(config)
    before = database_state(config.data / "knowledge" / "knowledge.sqlite")
    plan = preflight(config, deployment_id="project-knowledge", owner="fixture")
    assert plan["source_state"]["digest"] == before["digest"]
    assert plan["source_state"]["documents"] == 1
    assert plan["bindings"] and all(item["agent_id"] for item in plan["bindings"])
    assert not plan["unmapped_actors"]
    # No deployment, no target data directory, source unchanged.
    assert ServiceStore(config.data).deployments() == []
    assert not (config.data / "deployments" / "project-knowledge" / "data").exists()
    assert database_state(config.data / "knowledge" / "knowledge.sqlite")["digest"] == before["digest"]


def test_migration_preserves_data_maps_bindings_and_serves_real_mcp(managed):
    config = managed.config
    seeded = seed_knowledge(config)
    plan = preflight(config, deployment_id="project-knowledge", owner="fixture")
    operation = apply(config, plan)
    assert operation["status"] == "succeeded", operation
    stores = ServiceStore(config.data)
    deployment = stores.deployment("project-knowledge")
    assert deployment["publish_state"] == "published" and deployment["verification"] == "business_verified"
    assert deployment["config"]["builtin"] == "knowledge"
    source = database_state(config.data / "knowledge" / "knowledge.sqlite")
    target = database_state(Path(deployment["config"]["data_root"]) / "knowledge.sqlite")
    for key in ("namespaces", "documents", "versions", "submissions"):
        assert source[key] == target[key], key
    assert target["digest"] == plan["source_state"]["digest"] and target["search"] == 1
    assert Path(deployment["config"]["data_root"]) != config.data / "knowledge"
    receipt = json.loads(Path(operation["plan"]["result"]["receipt"]).read_text(encoding="utf-8"))
    assert receipt["source"]["summary"]["digest"] == receipt["target"]["summary"]["digest"]
    assert receipt["verification"] == "business_verified"

    # Legacy actors became visible platform bindings with their original scopes.
    identity = stores.agent_identity_for_source(REPO_ACTOR)
    binding = next(item for item in stores.bindings(agent_id=identity["id"],
                                                    deployment_id="project-knowledge") if item["enabled"])
    assert binding["scope"]["namespaces"] == [seeded["namespace"]["id"]]
    assert "knowledge_submit" in binding["tools"]

    token, deployment = agent_token(config, identity["id"])
    answers = call(deployment["endpoint"], token, [
        ("knowledge_catalog", {}),
        ("knowledge_search", {"namespace": seeded["namespace"]["id"], "query": "规则"})])
    catalog = json.loads(answers[0].content[0].text)
    assert [item["slug"] for item in catalog["namespaces"]] == ["project-a"]
    search = json.loads(answers[1].content[0].text)
    assert any(item["id"] == seeded["document"]["id"] for item in search["items"])
    # The admin actor keeps its own migrated submission scope.
    admin_token, deployment = agent_token(config, "admin")
    pending = json.loads(call(deployment["endpoint"], admin_token, [
        ("knowledge_submission_list", {"namespace": seeded["namespace"]["id"], "status": "pending"})])[0].content[0].text)
    assert any(item["id"] == seeded["submission"]["id"] for item in pending["items"])
    # Project isolation: a namespace outside the binding is not visible.
    store = Knowledge(config, deployment["config"]["data_root"])
    hidden = store.save_namespace({"slug": "hidden", "name": "不可见", "description": ""})
    store.publish(hidden["id"], {"title": "隐藏规则", "summary": "隐藏", "body_markdown": "# 隐藏\n正文"})
    denied = call(deployment["endpoint"], token, [
        ("knowledge_search", {"namespace": hidden["id"], "query": "隐藏"})])[0]
    assert denied.isError or "不可见" in denied.content[0].text or "404" in denied.content[0].text
    assert verify(config)["ok"]

    # Persistence across a controlled restart: one new generation, same data.
    managed.stop("project-knowledge", reason="fixture")
    started = managed.start("project-knowledge", reason="fixture")
    assert started["generation"] == deployment["generation"] + 1
    token, started = agent_token(config, identity["id"])
    again = json.loads(call(started["endpoint"], token, [
        ("knowledge_search", {"namespace": seeded["namespace"]["id"], "query": "发布规则"})])[0].content[0].text)
    assert again["items"], again


def test_unbind_revokes_knowledge_credentials(config, managed):
    config = managed.config
    seeded = seed_knowledge(config)
    plan = preflight(config, deployment_id="project-knowledge")
    assert apply(config, plan)["status"] == "succeeded"
    stores = ServiceStore(config.data)
    identity = stores.agent_identity_for_source(REPO_ACTOR)
    token, deployment = agent_token(config, identity["id"])
    assert call(deployment["endpoint"], token, [("knowledge_catalog", {})])
    # A second live run gets its own credential without invalidating the first.
    second_token, _ = agent_token(config, identity["id"])
    knowledge_binding = Knowledge(config, deployment["config"]["data_root"]).binding(
        "agent:" + identity["id"])
    assert knowledge_binding["revision"] == 1
    assert call(deployment["endpoint"], token, [("knowledge_catalog", {})])
    assert call(deployment["endpoint"], second_token, [("knowledge_catalog", {})])
    binding = next(item for item in stores.bindings(agent_id=identity["id"],
                                                    deployment_id="project-knowledge") if item["enabled"])
    ServiceCatalog(config).unbind(binding["id"])
    with pytest.raises(BaseException):
        call(deployment["endpoint"], token, [("knowledge_catalog", {})])
    # A disabled binding cannot mint a new credential either.
    disabled = stores.binding(binding["id"])
    with pytest.raises(ValueError, match="停用"):
        builtin_grant(config, deployment, disabled, deadline=time.time() + 60)


def test_source_change_and_duplicate_owner_are_rejected(config, managed):
    config = managed.config
    seed_knowledge(config)
    plan = preflight(config, deployment_id="project-knowledge")
    store = Knowledge(config)
    store.publish(store.namespace("project-a")["id"],
                  {"title": "计划后新增", "summary": "变化", "body_markdown": "# 变化\n内容"})
    with pytest.raises(ValueError, match="来源知识库在计划生成后发生变化"):
        apply(config, plan)

    plan = preflight(config, deployment_id="project-knowledge")
    assert apply(config, plan)["status"] == "succeeded"
    stores = ServiceStore(config.data)
    target = stores.deployment("project-knowledge")["config"]["data_root"]
    stores.create_deployment("knowledge-alias", service_id="project-knowledge", mode="managed",
                             config={"builtin": "knowledge", "data_root": target})
    with pytest.raises(ValueError, match="同一数据目录已有监督归属"):
        preflight(config, deployment_id="project-knowledge")


def test_reapply_reuses_data_and_single_generation(config, managed):
    config = managed.config
    seed_knowledge(config)
    first = apply(config, preflight(config, deployment_id="project-knowledge"))
    assert first["status"] == "succeeded"
    deployment = ServiceStore(config.data).deployment("project-knowledge")
    second = apply(config, preflight(config, deployment_id="project-knowledge"))
    assert second["status"] == "succeeded", second
    after = ServiceStore(config.data).deployment("project-knowledge")
    assert after["generation"] == deployment["generation"]
    assert after["gateway_pid"] == deployment["gateway_pid"]
    assert verify(config)["ok"]


def test_unmapped_legacy_actor_blocks_migration(config, managed):
    config = managed.config
    seed_knowledge(config)
    store = Knowledge(config)
    other = store.save_namespace({"slug": "project-b", "name": "项目 B", "description": ""})
    store.bind("repo:" + "b" * 16, {"primary_namespace_id": other["id"],
                                    "read_namespace_ids": [other["id"]], "submit_namespace_ids": []})
    plan = preflight(config, deployment_id="project-knowledge")
    assert plan["unmapped_actors"] == ["repo:" + "b" * 16]
    with pytest.raises(ValueError, match="未映射的来源主体"):
        apply(config, plan)
    assert ServiceStore(config.data).deployments() == []


def test_in_place_takeover_and_rollback(config, managed):
    config = managed.config
    seeded = seed_knowledge(config)
    legacy = KnowledgeService(config).ensure(explicit=True)
    assert legacy["state"] == "healthy"
    plan = preflight(config, deployment_id="project-knowledge", in_place=True)
    operation = apply(config, plan)
    assert operation["status"] == "succeeded", operation
    deployment = ServiceStore(config.data).deployment("project-knowledge")
    assert deployment["config"]["in_place"] is True
    assert deployment["config"]["data_root"] == str(config.data / "knowledge")
    assert deployment["gateway_pid"] and deployment["gateway_pid"] != legacy["pid"]
    result = verify(config)
    assert result["ok"], result
    owners = next(check for check in result["checks"] if check["name"] == "同一数据目录只有一个活动监督者")
    assert len({item.get("pid") for item in owners["owners"]}) == 1

    rolled = rollback(config, reason="fixture")
    assert rolled["deployment"]["desired_state"] == "stopped"
    assert KnowledgeService(config).status()["state"] == "healthy"
    current = Knowledge(config, config.data / "knowledge")
    assert json.loads(call(KnowledgeService(config).status()["endpoint"],
                           Knowledge(config).issue("admin", {"actor": "local-admin",
                                                             "read_namespace_ids": [seeded["namespace"]["id"]],
                                                             "submit_namespace_ids": []}, 60),
                           [("knowledge_catalog", {})])[0].content[0].text)["namespaces"]


def test_resolver_mounts_shared_knowledge_once(config, managed):
    config = managed.config
    seeded = seed_knowledge(config)
    assert apply(config, preflight(config, deployment_id="project-knowledge"))["status"] == "succeeded"
    stores = ServiceStore(config.data)
    identity = stores.agent_identity_for_source(REPO_ACTOR)
    manifest = {"schema_version": 3, "kind": "agent", "services": [
        {"alias": "knowledge", "service": "project-knowledge", "contract": "project-knowledge/v1",
         "permissions": ["knowledge_catalog", "knowledge_search"], "required": True}]}
    patch = config.data / "v3-knowledge.patch.yml"
    patch.write_text("[]\n", encoding="utf-8")
    workspace = config.data / "v3-workspace"
    (workspace / ".agent").mkdir(parents=True, exist_ok=True)
    (workspace / ".agent" / "resources.json").write_text("{}", encoding="utf-8")
    env = {}
    instructions = ServiceResolver(config).attach(
        patch=patch, env=env, workspace=workspace, manifest=manifest,
        installation={"repo_id": REPO_ACTOR.removeprefix("repo:")}, deadline=time.time() + 120)
    assert "knowledge" in instructions
    rows = yaml.load(patch.read_text(), Loader=PatchLoader)
    inserts = [insert for row in rows for insert in row.get("insert", [])]
    assert len([item for item in inserts if item["config"]["serverName"] == "knowledge"]) == 1
    assert inserts[0]["config"]["url"] == stores.deployment("project-knowledge")["endpoint"]
    assert env["AGENT_BASE_SERVICE_KNOWLEDGE_AUTH"].startswith("Bearer ")
    catalog = json.loads((workspace / ".agent" / "resources.json").read_text())
    assert catalog["services"][0]["service_id"] == "project-knowledge"
    # The compatibility attach path must not mount a second knowledge client.
    from agent_base.knowledge_service import attach
    assert attach(config, patch, env, REPO_ACTOR, {"turn_id": "fixture"}, time.time() + 60) == ""
    assert len([insert for row in yaml.load(patch.read_text(), Loader=PatchLoader)
                for insert in row.get("insert", []) if insert["config"]["serverName"] == "knowledge"]) == 1

    # Missing required binding fails before any business side effect.
    unbound = dict(manifest)
    stores.ensure_agent_identity(source="repo:" + "c" * 16, name="unbound")
    with pytest.raises(ValueError, match="必需的共享服务"):
        ServiceResolver(config).attach(
            patch=patch, env=env, workspace=workspace, manifest=unbound,
            installation={"repo_id": "c" * 16}, deadline=time.time() + 60)


def test_supervisor_probes_and_recovers_builtin_knowledge(config, managed):
    from agent_base.processes import kill_tree
    from agent_base.service_supervisor import ServiceSupervisor
    config = managed.config
    seed_knowledge(config)
    assert apply(config, preflight(config, deployment_id="project-knowledge"))["status"] == "succeeded"
    supervisor = ServiceSupervisor(config)
    first = supervisor.probe("project-knowledge", business=True)
    assert first["state"] == "healthy"
    assert first["evidence"]["process"] and first["evidence"]["protocol"]
    assert first["evidence"]["business"]["ok"]
    deployment = ServiceStore(config.data).deployment("project-knowledge")
    kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
    states = [supervisor.probe("project-knowledge", business=False)["state"] for _ in range(3)]
    assert states == ["degraded", "degraded", "unhealthy"]
    recovered = ServiceStore(config.data).deployment("project-knowledge")
    assert recovered["generation"] == deployment["generation"] + 1
    assert recovered["recovery_attempts"] == 1
    assert ManagedServices(config).gateway_health(recovered)["gateway_alive"]
    followups = [supervisor.probe("project-knowledge", business=False)["state"] for _ in range(3)]
    assert followups == ["degraded", "healthy", "healthy"]
    incident = ServiceStore(config.data).incidents(deployment_id="project-knowledge")[0]
    assert incident["status"] == "closed" and incident["close_evidence"]
