"""Knowledge service takeover: preflight, explicit migration and verification.

The platform knowledge MCP becomes a shared managed deployment in the service
catalog. Migration never copies source code: it freezes a plan, snapshots the
live SQLite database with the online backup API, verifies the snapshot against
the source, starts exactly one owner for the new data root, maps legacy
knowledge bindings to platform Agent identities and publishes the deployment.
The legacy `KnowledgeService` path stays as the compatibility behaviour for
v1/v2 repos until an operator explicitly adopts the shared deployment.

Isolated state only: this module never stops or rewrites a business knowledge
instance unless an explicit in-place plan is requested and acknowledged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .knowledge import Knowledge
from .knowledge_mcp import TOOLS as KNOWLEDGE_TOOLS
from .knowledge_service import KnowledgeService
from .processes import alive, identity
from .repositories import installation_lock
from .service_store import ServiceStore

SERVICE_ID = "project-knowledge"
CONTRACT = "project-knowledge/v1"
ALIAS = "knowledge"
TOOL_NAMES = [tool["name"] for tool in KNOWLEDGE_TOOLS]
READ_TOOLS = [name for name in TOOL_NAMES if name != "knowledge_submit"]
HEALTH = {"interval_seconds": 30, "timeout_seconds": 5, "failure_threshold": 3,
          "recovery_threshold": 2, "startup_timeout_seconds": 30}


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def builtin_manifest() -> dict:
    """Synthetic revision manifest: a platform component, not a Git checkout."""
    return {
        "schema_version": 3, "kind": "mcp-service",
        "service": {"id": SERVICE_ID, "version": "1.0.0", "contract": CONTRACT,
                    "title": "项目知识库"},
        "runtime": {"transport": "streamable_http"},
        "health": {**HEALTH, "expected_tools": TOOL_NAMES,
                   "probe_tool": {"tool": "knowledge_catalog", "arguments": {}}},
        "operations": {"restart_safe": True, "session_sharing": "safe",
                       "runbook": "docs/Knowledge.md"},
    }


def is_builtin(deployment: dict | None) -> bool:
    return bool(deployment and deployment.get("config", {}).get("builtin") == "knowledge")


def builtin_root(config, deployment: dict) -> Path:
    root = deployment.get("config", {}).get("data_root")
    if not root:
        raise ValueError("内置知识库部署缺少 data_root")
    return Path(root)


def builtin_deployments(config) -> list[dict]:
    return [item for item in ServiceStore(config.data).deployments() if is_builtin(item)]


def latest_builtin(config, *, published: bool = True) -> dict | None:
    items = builtin_deployments(config)
    if published:
        items = [item for item in items if item["publish_state"] == "published"]
    return items[-1] if items else None


# -- state inspection ---------------------------------------------------------
def database_state(path: Path) -> dict:
    """Deterministic business-data snapshot used for before/after comparison."""
    state = {"exists": Path(path).is_file(), "namespaces": [], "documents": [], "versions": [],
             "submissions": [], "audit": 0, "bindings": [], "search": None}
    if not state["exists"]:
        state["digest"] = _digest(state)
        return state
    connection = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        for row in connection.execute("SELECT id,slug,name,description,enabled FROM namespaces ORDER BY id"):
            state["namespaces"].append(dict(row))
        for row in connection.execute("SELECT id,namespace_id,slug,status,current_version_id,updated FROM documents ORDER BY id"):
            state["documents"].append(dict(row))
        for row in connection.execute("SELECT id,document_id,number,parent_id,candidate,source,created FROM versions ORDER BY id"):
            state["versions"].append(dict(row))
        for row in connection.execute("SELECT id,namespace_id,operation,status,content_hash,version_id,actor,created FROM submissions ORDER BY id"):
            state["submissions"].append(dict(row))
        state["audit"] = connection.execute("SELECT count(*) FROM audit").fetchone()[0]
        for row in connection.execute("SELECT actor,definition,revision FROM bindings ORDER BY actor"):
            state["bindings"].append(dict(row))
        try:
            state["search"] = connection.execute(
                "SELECT count(*) FROM search").fetchone()[0]
        except sqlite3.OperationalError:
            state["search"] = None
    finally:
        connection.close()
    state["digest"] = _digest({key: state[key] for key in
                               ("namespaces", "documents", "versions", "submissions")})
    return state


def _summary(state: dict) -> dict:
    statuses = {}
    for item in state["submissions"]:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1
    return {"namespaces": len(state["namespaces"]), "documents": len(state["documents"]),
            "versions": len(state["versions"]), "submissions": statuses,
            "audit": state["audit"], "digest": state["digest"],
            "search_index": state["search"]}


def _source_db(config) -> Path:
    return config.data / "knowledge" / "knowledge.sqlite"


def _target_root(config, deployment_id: str) -> Path:
    return config.data / "deployments" / deployment_id / "data" / "knowledge"


def _plan_digest(plan: dict) -> str:
    body = {key: value for key, value in plan.items() if key not in {"created", "plan_digest"}}
    return _digest(body)


def _binding_tools(scope: dict) -> list[str]:
    submit = scope.get("submit_namespace_ids") or scope.get("submit") or []
    tools = [name for name in READ_TOOLS]
    if submit:
        tools.append("knowledge_submit")
    return [name for name in TOOL_NAMES if name in tools]


# -- preflight ---------------------------------------------------------------
def preflight(config, *, deployment_id: str = SERVICE_ID, owner: str = "", publish: bool = True,
              in_place: bool = False, alias: str = ALIAS) -> dict:
    """Read-only frozen plan for adopting the knowledge service."""
    if not isinstance(deployment_id, str) or not deployment_id:
        raise ValueError("需要 deployment_id")
    if not isinstance(alias, str) or not alias:
        raise ValueError("需要绑定别名")
    stores = ServiceStore(config.data)
    existing = stores.deployment(deployment_id)
    if existing and not is_builtin(existing):
        raise ValueError(f"部署 {deployment_id} 已存在且不是内置知识库部署")
    source_root = config.data / "knowledge"
    source_db = _source_db(config)
    if not source_db.is_file():
        raise ValueError("没有可迁移的知识库数据：缺少 knowledge/knowledge.sqlite")
    target_root = source_root if in_place else _target_root(config, deployment_id)
    conflicts = [item["id"] for item in builtin_deployments(config)
                 if Path(item["config"].get("data_root", "")) == target_root and item["id"] != deployment_id]
    if conflicts:
        raise ValueError(f"同一数据目录已有监督归属：{conflicts}；不能同时启动两个管理器")
    state = database_state(source_db)
    legacy = KnowledgeService(config)
    status = legacy.status()
    identities = {}
    bindings = []
    missing = []
    for item in state["bindings"]:
        actor = item["actor"]
        if actor == "admin":
            agent = stores.agent_identity("admin") or stores.ensure_agent_identity(source="admin", name="Admin",
                                                                                  kind="builtin")
        elif actor.startswith("repo:"):
            agent = stores.agent_identity_for_source(actor)
        else:
            agent = None
        if agent is None:
            missing.append(actor)
            continue
        scope = json.loads(item["definition"])
        scope = {**scope, "required": bool(scope.get("required")), "legacy_actor": actor,
                 "legacy_revision": item["revision"]}
        identities[actor] = agent["id"]
        bindings.append({"agent_id": agent["id"], "agent_name": agent.get("name", agent["id"]),
                         "legacy_actor": actor, "scope": scope, "tools": _binding_tools(scope)})
    risks = []
    if not state["namespaces"]:
        risks.append("来源知识库没有项目；迁移后需要先创建项目再授权 Agent")
    if in_place:
        risks.append("就地接管会先停止原知识库进程；执行前需确认没有活动对话/任务")
    if status["state"] in {"healthy", "unhealthy"}:
        risks.append("原知识库实例正在运行：新部署使用独立数据目录时两者可并存，但同一数据目录只能有一个监督者")
    if existing:
        risks.append("部署已存在：本次为配置/数据复核，不新增第二个实例")
    plan = {"kind": "knowledge-migration", "deployment_id": deployment_id,
            "service_id": SERVICE_ID, "contract": CONTRACT, "alias": alias,
            "owner": owner, "publish": bool(publish), "in_place": bool(in_place),
            "source_root": str(source_root), "target_root": str(target_root),
            "source_state": _summary(state), "source_bindings": state["bindings"],
            "bindings": bindings, "unmapped_actors": missing,
            "legacy_instance": {"pid": status.get("pid"), "birth": status.get("birth"),
                                "endpoint": status.get("endpoint"), "state": status["state"]},
            "supervision": {"legacy_owner": "knowledge_service" if status["state"] in {"healthy", "unhealthy"} else None,
                            "existing_deployment": existing["id"] if existing else None,
                            "target_owner": deployment_id},
            "backup_dir": str(config.data / "deployments" / deployment_id / "backups"),
            "tools": TOOL_NAMES, "health": HEALTH, "risks": risks, "created": time.time()}
    plan["plan_digest"] = _plan_digest(plan)
    return plan


# -- execution ---------------------------------------------------------------
def apply(config, plan: dict, *, idempotency_key: str | None = None) -> dict:
    """Execute a frozen plan under the deployment lock; safe to re-check."""
    if plan.get("kind") != "knowledge-migration":
        raise ValueError("不是知识库迁移计划")
    if plan.get("plan_digest") != _plan_digest(plan):
        raise ValueError("迁移计划已被修改，请重新准备计划")
    current = database_state(_source_db(config))
    if current["digest"] != plan["source_state"]["digest"]:
        raise ValueError("来源知识库在计划生成后发生变化；请重新 prepare 冻结计划，不会静默换版本")
    if plan.get("unmapped_actors"):
        raise ValueError(f"存在未映射的来源主体，先登记对应 Agent 身份：{plan['unmapped_actors']}")
    stores = ServiceStore(config.data)
    deployment_id = plan["deployment_id"]
    operation = stores.create_operation(deployment_id=deployment_id, kind="knowledge_migration",
                                        plan=plan, idempotency_key=idempotency_key)
    if operation.get("duplicate"):
        return operation
    root = config.data / "deployments" / deployment_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "logs").mkdir(exist_ok=True)
    # A migration lock, not the deployment lock: start/stop acquire the latter
    # themselves and the lock implementation is deliberately not reentrant.
    with installation_lock(root / "migration.lock", 300):
        try:
            _execute(config, stores, operation, plan, root, current)
        except BaseException as error:
            stores.update_operation(operation["id"], status="failed", error=str(error)[:1000])
            raise
    return stores.operation(operation["id"])


def _execute(config, stores: ServiceStore, operation: dict, plan: dict, root: Path, source_state: dict):
    operation_id = operation["id"]
    progress = {**(operation["plan"].get("progress") or {})}

    def step(name: str, **state):
        stores.update_operation(operation_id, status=state.pop("status", "installing"),
                                plan={**stores.operation(operation_id)["plan"],
                                      "progress": {**progress, name: state or True}})
        progress[name] = state or True

    stores.update_operation(operation_id, status="installing", pid=os.getpid(), birth=identity(os.getpid()))
    target_root = Path(plan["target_root"])
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    in_place = bool(plan["in_place"])

    # 1. Online snapshot through the SQLite backup API (active WAL safe).
    backups = Path(plan["backup_dir"])
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backups / ("knowledge-" + time.strftime("%Y%m%d-%H%M%S") + ".sqlite")
    if not progress.get("backup"):
        _snapshot(config.data / "knowledge" / "knowledge.sqlite", backup)
        verified = database_state(backup)
        if verified["digest"] != source_state["digest"]:
            raise ValueError("备份核对失败：快照内容与来源不一致，未开始切换")
        step("backup", path=str(backup), digest=verified["digest"])

    # 2. Materialize the deployment data root at the frozen target.
    if in_place:
        if not progress.get("data"):
            if target_root == config.data / "knowledge":
                step("data", mode="in_place", path=str(target_root))
            else:
                raise ValueError("就地接管必须使用原知识库数据目录")
    else:
        target_db = target_root / "knowledge.sqlite"
        reuse = database_state(target_db)
        if reuse["exists"] and reuse["digest"] == source_state["digest"]:
            # A previous attempt already produced a verified copy; safely repeat
            # without touching the live source.
            step("data", path=str(target_db), digest=reuse["digest"], resumed=True)
        elif not progress.get("data"):
            legacy = KnowledgeService(config)
            status = legacy.status()
            if status["state"] == "healthy":
                # Drain: stop the old owner briefly so the copy is quiescent,
                # then restart it. The source remains the single business owner
                # until the new deployment passes validation.
                legacy.stop()
                try:
                    _snapshot(config.data / "knowledge" / "knowledge.sqlite", target_db)
                finally:
                    restored = KnowledgeService(config).ensure(explicit=True)
                    if restored["state"] != "healthy":
                        raise ValueError("拷贝后原知识库未能恢复；请人工核对后再继续")
            else:
                _snapshot(config.data / "knowledge" / "knowledge.sqlite", target_db)
            copied = database_state(target_db)
            if copied["digest"] != source_state["digest"]:
                raise ValueError("迁移数据核对失败：目标内容与来源不一致，未切换监督归属")
            step("data", path=str(target_db), digest=copied["digest"])
        elif not reuse["exists"]:
            raise ValueError("迁移进度显示数据已复制，但目标数据库不存在；需要人工核对")

    # 3. Register the platform revision and deployment record.
    if not progress.get("registration"):
        stores.save_service(SERVICE_ID, title="项目知识库")
        manifest = builtin_manifest()
        revision_root = root / "revision"
        revision_root.mkdir(parents=True, exist_ok=True)
        (revision_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
        revision = stores.save_revision(service_id=SERVICE_ID, origin="builtin:agent-base",
                                        commit=(config.runtime.get("model") or "builtin")[:12] or "builtin",
                                        component="knowledge", manifest=manifest,
                                        root=revision_root, python=sys.executable)
        deployment = stores.deployment(plan["deployment_id"])
        config_fields = {"builtin": "knowledge", "data_root": str(target_root),
                         "owner": plan.get("owner", ""), "title": "项目知识库",
                         "contract": CONTRACT, "tools": TOOL_NAMES, "health": HEALTH,
                         "in_place": in_place, "legacy_root": str(config.data / "knowledge")}
        if deployment is None:
            stores.create_deployment(plan["deployment_id"], service_id=SERVICE_ID, mode="managed",
                                     config=config_fields, revision_id=revision["id"])
        else:
            stores.update_deployment(plan["deployment_id"], revision_id=revision["id"], config=config_fields,
                                     config_revision=deployment["config_revision"] + 1)
        step("registration", revision_id=revision["id"])

    # 4. Start exactly one owner for the target data root.
    from .service_install import ManagedServices
    managed = ManagedServices(config)
    stores.update_operation(operation_id, status="validating")
    if not progress.get("startup"):
        started = managed.start(plan["deployment_id"], reason=f"knowledge-migration:{operation_id}")
        step("startup", generation=started["generation"], endpoint=started["endpoint"])

    # 5. Real MCP validation (initialize, tool directory, read-only catalog).
    if not progress.get("validation"):
        result = managed.validate(plan["deployment_id"], business=True)
        if not result["ok"]:
            raise ValueError(f"迁移部署验证失败：{result['errors']}")
        stores.update_deployment(plan["deployment_id"], verification=result["verification"],
                                 observed_state="healthy", observed_at=time.time(),
                                 observed_evidence=encoded({"kind": "knowledge_migration", **result}))
        step("validation", verification=result["verification"], tools=result["tools"])

    # 6. Map legacy knowledge bindings to explicit platform bindings.
    if not progress.get("bindings"):
        target_store = Knowledge(config, target_root)
        mapped = []
        for item in plan["bindings"]:
            scope = {"namespaces": item["scope"].get("read_namespace_ids", []),
                     "submit": item["scope"].get("submit_namespace_ids", []),
                     "primary": item["scope"].get("primary_namespace_id"),
                     "required": bool(item["scope"].get("required")),
                     "legacy_actor": item["scope"].get("legacy_actor"),
                     "legacy_revision": item["scope"].get("legacy_revision")}
            binding = stores.save_binding(agent_id=item["agent_id"], deployment_id=plan["deployment_id"],
                                          alias=plan["alias"], service_id=SERVICE_ID, contract=CONTRACT,
                                          tools=item["tools"], scope=scope)
            _mirror_knowledge_binding(target_store, binding)
            mapped.append({"binding_id": binding["id"], "agent_id": item["agent_id"],
                           "legacy_actor": item["scope"].get("legacy_actor"), "tools": item["tools"]})
        step("bindings", mapped=mapped)

    # 7. Publication is separate from authorization.
    if plan.get("publish") and not progress.get("publish"):
        from .service_catalog import ServiceCatalog
        catalog = ServiceCatalog(config)
        if stores.deployment(plan["deployment_id"])["publish_state"] != "published":
            catalog.publish(plan["deployment_id"], actor=f"knowledge-migration:{operation_id}")
        step("publish", publish_state="published")

    # 8. Receipt with before/after evidence.
    after = database_state(target_root / "knowledge.sqlite")
    if after["digest"] != source_state["digest"]:
        raise ValueError("收据核对失败：迁移后业务数据摘要与迁移前不一致")
    receipt = {"kind": "knowledge-migration", "operation_id": operation_id,
               "deployment_id": plan["deployment_id"], "service_id": SERVICE_ID,
               "contract": CONTRACT, "in_place": in_place,
               "source": {"root": plan["source_root"], "summary": _summary(source_state)},
               "target": {"root": str(target_root), "summary": _summary(after)},
               "backup": str(backup), "plan_digest": plan["plan_digest"],
               "bindings": progress.get("bindings", []),
               "verification": progress.get("validation", {}).get("verification") if isinstance(
                   progress.get("validation"), dict) else None,
               "generation": stores.deployment(plan["deployment_id"])["generation"],
               "recorded_at": time.time()}
    receipts = root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_file = receipts / f"{operation_id}.json"
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    stores.update_operation(operation_id, status="succeeded",
                            plan={**stores.operation(operation_id)["plan"],
                                  "result": {**receipt, "receipt": str(receipt_file)}})


def _snapshot(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        target.unlink()
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=30)
    target_connection = sqlite3.connect(str(target))
    try:
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        target_connection.close()
        source_connection.close()


def _mirror_knowledge_binding(store: Knowledge, binding: dict):
    """Keep the knowledge-side scope revision in step with the platform binding."""
    actor = knowledge_actor(binding["agent_id"])
    scope = binding["scope"]
    definition = {"primary_namespace_id": scope.get("primary"),
                  "read_namespace_ids": scope.get("namespaces", []),
                  "submit_namespace_ids": scope.get("submit", []),
                  "required": bool(scope.get("required")), "enabled": True}
    current = store.binding(actor)
    if current and all(current.get(key) == value for key, value in definition.items()):
        # Same scope: reuse the revision so issuing a new run's credential does
        # not invalidate tokens held by another live run.
        return current
    definition["expected_revision"] = current["revision"] if current else 0
    return store.bind(actor, definition)


def knowledge_actor(agent_id: str) -> str:
    return "admin" if agent_id == "admin" else "agent:" + agent_id


# -- read models / operations ------------------------------------------------
def _operation_summary(config, deployment_id: str) -> dict | None:
    operations = ServiceStore(config.data).operations(deployment_id=deployment_id, limit=1)
    return operations[0] if operations else None


def summary(config, deployment_id: str = SERVICE_ID) -> dict:
    stores = ServiceStore(config.data)
    deployment = stores.deployment(deployment_id)
    if not deployment:
        raise ValueError(f"部署不存在：{deployment_id}")
    if not is_builtin(deployment):
        raise ValueError("该部署不是内置知识库部署")
    root = builtin_root(config, deployment)
    legacy = KnowledgeService(config)
    return {"deployment": deployment, "legacy_instance": legacy.status(),
            "target_state": _summary(database_state(root / "knowledge.sqlite")),
            "source_state": _summary(database_state(_source_db(config))),
            "operation": _operation_summary(config, deployment_id),
            "supervision": {"target_owner": deployment_id,
                            "target_process": {"pid": deployment.get("gateway_pid"),
                                               "birth": deployment.get("gateway_birth"),
                                               "alive": alive(deployment.get("gateway_pid"),
                                                              deployment.get("gateway_birth"))},
                            "legacy_owner": "knowledge_service"}}


def verify(config, deployment_id: str = SERVICE_ID) -> dict:
    """Check that migrated business records survive and one owner remains.

    The target legitimately receives new knowledge after the switch, so this
    checks containment of the migrated records plus integrity and ownership;
    the freeze-time equality receipt lives in `receipts/`.
    """
    stores = ServiceStore(config.data)
    deployment = stores.deployment(deployment_id)
    if not deployment or not is_builtin(deployment):
        raise ValueError(f"内置知识库部署不存在：{deployment_id}")
    root = builtin_root(config, deployment)
    target_db = root / "knowledge.sqlite"
    target = database_state(target_db)
    source = database_state(_source_db(config))
    checks = []
    integrity = None
    if target_db.is_file():
        connection = sqlite3.connect(f"file:{target_db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
    checks.append({"name": "目标数据库完整", "ok": integrity == "ok", "integrity_check": integrity})
    missing = []
    target_namespaces = {item["id"]: item for item in target["namespaces"]}
    for item in source["namespaces"]:
        current = target_namespaces.get(item["id"])
        if not current or current["slug"] != item["slug"] or current["enabled"] != item["enabled"]:
            missing.append("namespace:" + item["id"])
    target_versions = {item["id"] for item in target["versions"]}
    for item in source["versions"]:
        if item["id"] not in target_versions:
            missing.append("version:" + item["id"])
    target_submissions = {item["id"] for item in target["submissions"]}
    for item in source["submissions"]:
        if item["id"] not in target_submissions:
            missing.append("submission:" + item["id"])
    checks.append({"name": "迁移业务记录完整", "ok": not missing, "missing": missing,
                   "source": _summary(source), "target": _summary(target)})
    owners = []
    status = KnowledgeService(config).status()
    if status["state"] in {"healthy", "unhealthy"}:
        owners.append({"owner": "knowledge_service", "root": str(config.data / "knowledge"),
                       "pid": status.get("pid")})
    if alive(deployment.get("gateway_pid"), deployment.get("gateway_birth")):
        owners.append({"owner": "deployment", "root": str(root), "pid": deployment.get("gateway_pid")})
    checks.append({"name": "目标部署有一个活动监督者",
                   "ok": len([item for item in owners if item["owner"] == "deployment"]) == 1
                         and deployment["desired_state"] == "running", "owners": owners})
    same_root = Path(deployment["config"]["data_root"]) == config.data / "knowledge"
    if same_root:
        checks.append({"name": "同一数据目录只有一个活动监督者",
                       "ok": len({item.get("pid") for item in owners}) <= 1, "owners": owners})
    else:
        checks.append({"name": "来源与目标数据目录分离", "ok": True,
                       "source_root": str(config.data / "knowledge"),
                       "target_root": deployment["config"]["data_root"]})
    receipts = sorted((config.data / "deployments" / deployment_id / "receipts").glob("*.json"))
    if receipts:
        receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
        checks.append({"name": "迁移时冻结的数据摘要一致",
                       "ok": receipt["source"]["summary"]["digest"] == receipt["target"]["summary"]["digest"],
                       "receipt": str(receipts[-1])})
    return {"ok": all(item["ok"] for item in checks), "deployment_id": deployment_id,
            "checks": checks, "operation": _operation_summary(config, deployment_id)}


def rollback(config, deployment_id: str = SERVICE_ID, *, reason: str = "") -> dict:
    """Stop the migrated deployment; data is kept. In-place adopts restore legacy."""
    stores = ServiceStore(config.data)
    deployment = stores.deployment(deployment_id)
    if not deployment or not is_builtin(deployment):
        raise ValueError(f"内置知识库部署不存在：{deployment_id}")
    from .service_install import ManagedServices
    try:
        ManagedServices(config).stop(deployment_id, reason=f"rollback:{reason}" or "rollback")
    except ValueError:
        pass
    if deployment["config"].get("in_place"):
        KnowledgeService(config).ensure(explicit=True)
    stores.create_operation(deployment_id=deployment_id, kind="knowledge_rollback",
                            plan={"reason": reason, "in_place": bool(deployment["config"].get("in_place"))},
                            status="succeeded")
    return summary(config, deployment_id)


# -- runtime integration -----------------------------------------------------
def start_builtin(managed, deployment: dict, reason: str) -> dict:
    """Start the deployment-owned knowledge process under the deployment lock."""
    root = builtin_root(managed.config, deployment)
    if deployment["config"].get("in_place"):
        legacy = KnowledgeService(managed.config)
        legacy_status = legacy.status()
        if legacy.data_root == root and legacy_status["state"] in {"healthy", "unhealthy"}:
            # Single owner handover: stop the compatibility manager before the
            # deployment generation starts from the same directory.
            legacy.stop()
    service = KnowledgeService(managed.config, root)
    status = service.status()
    if status["state"] == "healthy":
        managed.services.update_deployment(deployment["id"], observed_state="healthy",
                                           observed_at=time.time(), endpoint=status["endpoint"],
                                           observed_evidence=encoded({"kind": "builtin_already_running",
                                                                      "reason": reason}))
        return managed.services.deployment(deployment["id"])
    generation = deployment["generation"] + 1
    managed.services.update_deployment(deployment["id"], generation=generation, desired_state="running",
                                       observed_state="starting", observed_at=time.time(),
                                       observed_evidence=encoded({"kind": "starting", "reason": reason}))
    try:
        status = service.ensure(explicit=True)
    except ValueError as error:
        managed.services.update_deployment(deployment["id"], observed_state="unhealthy",
                                           observed_at=time.time(),
                                           observed_evidence=encoded({"kind": "startup_failed",
                                                                      "error": str(error)[:500]}))
        raise
    managed.services.update_deployment(deployment["id"], endpoint=status["endpoint"],
                                       gateway_pid=status.get("pid"), gateway_birth=status.get("birth"),
                                       observed_state="starting", observed_at=time.time(),
                                       observed_evidence=encoded({"kind": "builtin_started",
                                                                  "reason": reason,
                                                                  "stats": status.get("stats", {})}))
    managed.services.health_event(deployment["id"], "process", "started",
                                  {"generation": generation, "pid": status.get("pid")})
    return managed.services.deployment(deployment["id"])


def stop_builtin(managed, deployment: dict, reason: str, desired: str = "stopped") -> dict:
    service = KnowledgeService(managed.config, builtin_root(managed.config, deployment))
    service.stop()
    managed.services.update_deployment(deployment["id"], desired_state=desired, gateway_pid=None,
                                       gateway_birth=None, endpoint=None, observed_state="stopped",
                                       observed_at=time.time(),
                                       observed_evidence=encoded({"kind": "builtin_stopped", "reason": reason}))
    managed.services.health_event(deployment["id"], "process", "stopped", {"reason": reason})
    return managed.services.deployment(deployment["id"])


def builtin_health(config, deployment: dict) -> dict:
    service = KnowledgeService(config, builtin_root(config, deployment))
    status = service.status()
    alive_now = status["state"] in {"healthy", "unhealthy"} or alive(status.get("pid"), status.get("birth"))
    return {"gateway_alive": alive_now, "ready": status["state"] == "healthy",
            "protocol_ok": status["state"] == "healthy", "in_flight": 0,
            "state": status["state"], "stats": status.get("stats", {}),
            "backend": {"pid": status.get("pid"), "birth": status.get("birth"),
                        "endpoint": status.get("endpoint")},
            "deployment_id": deployment["id"], "generation": deployment["generation"]}


def _admin_grant(config, deployment: dict, seconds: float = 60) -> tuple[str, str]:
    service = KnowledgeService(config, builtin_root(config, deployment))
    status = service.status()
    if status["state"] != "healthy":
        raise ValueError(f"知识库部署当前不健康：{status['state']}")
    store = Knowledge(config, builtin_root(config, deployment))
    read = [item["id"] for item in store.namespaces()]
    token = store.issue("admin", {"actor": "local-admin", "read_namespace_ids": read,
                                  "submit_namespace_ids": []}, seconds)
    return token, status["endpoint"]


def validate_builtin(config, deployment: dict, *, business: bool = True) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx

    async def probe():
        token, endpoint = _admin_grant(config, deployment)
        tools: list[str] = []
        business_result = None
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    async with asyncio.timeout(20):
                        await session.initialize()
                        listing = await session.list_tools()
                        tools = [tool.name for tool in listing.tools]
                        if business:
                            answer = await session.call_tool("knowledge_catalog", {})
                            business_result = {"ok": not answer.isError,
                                               "content": answer.content[0].text[:200] if answer.content else None}
        return tools, business_result

    try:
        tools, business_result = asyncio.run(probe())
    except Exception as error:
        return {"ok": False, "errors": [f"协议验证失败：{type(error).__name__}: {str(error)[:300]}"],
                "verification": "not_verified", "tools": []}
    expected = builtin_manifest().get("health", {}).get("expected_tools", [])
    missing = [name for name in expected if name not in tools]
    errors = [f"缺少声明的工具：{missing}"] if missing else []
    if business_result and not business_result["ok"]:
        errors.append("业务探针 knowledge_catalog 失败")
    verification = "not_verified" if missing else (
        "business_verified" if business_result and business_result["ok"] else "protocol_verified")
    return {"ok": not errors, "errors": errors, "verification": verification if not errors else "not_verified",
            "tools": sorted(set(tools)), "business": business_result}


def probe_builtin_layers(config, deployment: dict, *, business: bool = True) -> dict:
    status = KnowledgeService(config, builtin_root(config, deployment)).status()
    process = status["state"] in {"healthy", "unhealthy"} or alive(status.get("pid"), status.get("birth"))
    layers = {"process": process, "protocol": False, "business": None,
              "instance": {"pid": status.get("pid"), "state": status["state"]},
              "stats": status.get("stats", {})}
    if not process:
        return layers
    token, endpoint = _admin_grant(config, deployment, 60) if status["state"] == "healthy" else (None, None)
    if not token:
        return layers

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx

    async def probe():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    async with asyncio.timeout(15):
                        await session.initialize()
                        listing = await session.list_tools()
                        tools = [tool.name for tool in listing.tools]
                        result = None
                        if business:
                            answer = await session.call_tool("knowledge_catalog", {})
                            result = {"ok": not answer.isError,
                                      "content": answer.content[0].text[:160] if answer.content else None}
                        return tools, result

    try:
        tools, result = asyncio.run(probe())
        layers["protocol"] = True
        layers["tools"] = tools
        if business:
            layers["business"] = result or {"ok": True, "namespaces": status.get("stats", {})}
        else:
            layers["business"] = {"ok": bool(status.get("stats")), "health_stats": status.get("stats")}
    except Exception as error:
        layers["error"] = f"{type(error).__name__}: {str(error)[:200]}"
    return layers


# -- explicit bindings for the generic resolver ------------------------------
def _find_binding(store: ServiceStore, agent_id: str, deployment_id: str) -> dict | None:
    for binding in store.bindings(agent_id=agent_id, deployment_id=deployment_id):
        if binding["enabled"] and binding["service_id"] == SERVICE_ID:
            return binding
    return None


def legacy_actor_binding(config, actor: str):
    """Compatibility routing for v1/v2 callers.

    `None` means no migrated deployment exists (legacy path applies). `False`
    means a migrated deployment exists but this actor has no explicit binding.
    A `(deployment, binding)` tuple mounts the shared deployment instead.
    """
    deployment = latest_builtin(config, published=True)
    if deployment is None:
        return None
    stores = ServiceStore(config.data)
    if actor == "admin":
        agent = stores.agent_identity("admin") or stores.ensure_agent_identity(source="admin", name="Admin",
                                                                               kind="builtin")
    elif isinstance(actor, str) and actor.startswith("repo:"):
        agent = stores.agent_identity_for_source(actor)
    else:
        agent = None
    if agent is None:
        return False
    binding = _find_binding(stores, agent["id"], deployment["id"])
    return (deployment, binding) if binding else False


def builtin_grant(config, deployment: dict, binding: dict, *, pid: int | None = None,
                  birth: float | None = None, deadline: float | None = None,
                  source: dict | None = None, run_id: str | None = None,
                  turn_id: str | None = None) -> str:
    """Issue a knowledge credential bound to the platform binding revision."""
    root = builtin_root(config, deployment)
    if not binding.get("enabled"):
        raise ValueError("知识库绑定已停用，不能签发访问凭证")
    store = Knowledge(config, root)
    scope = binding["scope"]
    definition = _mirror_knowledge_binding(store, binding)
    grant = {"actor": knowledge_actor(binding["agent_id"]), "revision": definition["revision"],
             "primary_namespace_id": definition.get("primary_namespace_id"),
             "read_namespace_ids": definition["read_namespace_ids"],
             "submit_namespace_ids": definition["submit_namespace_ids"],
             "pid": pid or os.getpid(), "birth": birth if birth is not None else identity(pid or os.getpid())}
    if run_id:
        grant["run_id"] = run_id
    if turn_id:
        grant["turn_id"] = turn_id
    if source and source.get("admin_session"):
        grant["admin_session"] = source["admin_session"]
    seconds = max(60.0, (deadline - time.time()) if deadline else 3600)
    return store.issue("agent", grant, seconds)


def revoke_builtin_grants(config, deployment: dict, agent_id: str):
    root = builtin_root(config, deployment)
    store = Knowledge(config, root)
    store.revoke_actor(knowledge_actor(agent_id))
