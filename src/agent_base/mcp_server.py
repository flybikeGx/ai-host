"""Native MCP endpoint for the administrator or one package's business tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import Config
from .engines import CREDENTIAL_NAME_MARKER, parse_credential_names, scrub_environment
from .package import expand, within
from .processes import run_process
from .review import REVIEW_DOCUMENTS
from .service import Service


def public(job: dict) -> dict:
    return {"installation_id": job["spec"].get("installation_id"), **{key: job.get(key) for key in ("id", "status", "created", "updated", "root", "error", "result", "session_id",
            "work_id", "workspace", "started_at", "finished_at", "deadline", "heartbeat", "session_status")}}


def schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
RUN = {"run_id": STRING}
STRING_MAP = {"type": "object", "additionalProperties": {"type": ["string", "object"]}}
PLAN_INPUT = {
    "origin": STRING, "ref": STRING, "component": STRING, "candidate_id": STRING,
    "deployment_id": STRING, "owner": STRING, "credentials": STRING_MAP, "env": STRING_MAP,
    "backend_headers": STRING_MAP, "auth_mode": {"type": "string", "enum": ["platform", "passthrough"]},
    "port": {"type": "integer", "minimum": 1024, "maximum": 65535},
    "publish": {"type": "boolean"}, "allow_guardian_restart": {"type": "boolean"},
}
ADMIN_TOOLS = {
    "prepare_service_install": ("只读准备：clone 仓库固定 commit（可指定组件相对路径），解析 schema v3 mcp-service 清单并生成安装计划；"
                                "报告缺失凭据、隔离声明与风险，不执行安装。返回 candidate_id 与计划摘要。",
                                schema(PLAN_INPUT, ("origin",))),
    "apply_service_install": ("执行安装计划：冻结源码、安装依赖与步骤、私有启动并完成 MCP 协议/业务探针验证；返回 operation_id。"
                              "后台执行，用 get_service_operation 查询；幂等键避免重复安装。发布需另行 publish_service。",
                                schema({**PLAN_INPUT, "idempotency_key": STRING, "background": {"type": "boolean"}}, ())),
    "get_service_operation": ("查询服务安装/发布/启停操作状态，可等待数秒。", schema({
        "operation_id": STRING, "deployment_id": STRING, "wait": {"type": "number", "minimum": 0, "maximum": 60}}, ())),
    "cancel_service_operation": ("请求取消进行中的服务操作；已执行步骤的结果不会自动回滚。", schema({"operation_id": STRING}, ("operation_id",))),
    "publish_service": ("将通过验证的部署登记到平台服务目录。发布不自动授权任何 Agent；需另行 bind_agent_service。", schema({
        "deployment_id": STRING}, ("deployment_id",))),
    "withdraw_service": ("撤销服务发布：阻止新接入并立即失效未用授权；不影响运行中的进程。", schema({
        "deployment_id": STRING, "reason": STRING}, ("deployment_id",))),
    "register_external_service": ("登记用户维护的外部 HTTP MCP 端点并执行真实 MCP 协议验证；平台只探测与路由，不管理远端进程。", schema({
        "deployment_id": STRING, "service_id": STRING, "url": STRING, "contract": STRING,
        "expected_tools": {"type": "array", "items": STRING}, "headers": STRING_MAP, "owner": STRING,
        "validate": {"type": "boolean"}}, ("deployment_id", "service_id", "url", "contract"))),
    "list_services": ("列出共享服务目录：托管/外部、发布与观测状态、验证等级、绑定和故障单。", schema({})),
    "get_service": ("查看一个部署的详情：契约、工具、绑定、健康事件与最近操作。", schema({"deployment_id": STRING}, ("deployment_id",))),
    "probe_service": ("立即对部署执行三层探测（进程/协议/业务依赖），记录健康事件。", schema({"deployment_id": STRING}, ("deployment_id",))),
    "read_service_log": ("读取部署日志末尾（已脱敏）。", schema({"deployment_id": STRING, "name": STRING,
        "tail_bytes": {"type": "integer", "minimum": 100, "maximum": 65536}}, ("deployment_id", "name"))),
    "list_service_incidents": ("列出故障单（可按部署过滤）。", schema({"deployment_id": STRING}, ())),
    "get_service_incident": ("查看一张故障单的诊断、动作与恢复证据。", schema({"incident_id": STRING}, ("incident_id",))),
    "start_service": ("启动托管部署（新代际），或启用外部服务的平台本机接入层；不会启动远端外部进程。", schema({"deployment_id": STRING}, ("deployment_id",))),
    "stop_service": ("停止托管部署并排空在途调用；外部服务只停用平台本机路由，不影响远端。数据目录保留。", schema({"deployment_id": STRING}, ("deployment_id",))),
    "restart_service": ("按策略重启托管部署。", schema({"deployment_id": STRING}, ("deployment_id",))),
    "list_service_agents": ("列出平台登记的 Agent 稳定身份及其来源映射。", schema({})),
    "prepare_knowledge_migration": ("只读准备知识库接管：列出原服务/PID、监督归属、数据目录、项目、旧绑定到平台 Agent 身份的映射，"
                                    "生成固定迁移计划（含数据摘要与风险）。不执行拷贝、不停止原服务。",
                                    schema({"deployment_id": STRING, "owner": STRING, "alias": STRING,
                                            "publish": {"type": "boolean"}, "in_place": {"type": "boolean"}}, ())),
    "apply_knowledge_migration": ("执行已冻结的知识库迁移计划：在线快照并核对数据、登记部署、启动唯一监督者、真实 MCP 验证、"
                                  "映射显式绑定并按计划发布；返回操作记录与收据。来源变化或计划过期会拒绝执行。",
                                  schema({"plan": {"type": "object"}, "idempotency_key": STRING}, ("plan",))),
    "get_knowledge_migration": ("查看知识库迁移部署的来源/目标数据摘要、原实例状态、监督归属与最近操作。",
                                schema({"deployment_id": STRING}, ())),
    "verify_knowledge_migration": ("核对迁移前后业务数据摘要、监督者数量与数据目录分离；不修改状态。",
                                   schema({"deployment_id": STRING}, ())),
    "rollback_knowledge_migration": ("停止迁移后的知识库部署并保留数据；就地接管会恢复原实例。",
                                     schema({"deployment_id": STRING, "reason": STRING}, ())),
    "bind_agent_service": ("为 Agent 稳定身份创建显式服务绑定：别名、契约、工具与数据范围。实际权限以绑定为准，repo 声明只是申请。",
                           schema({"agent_id": STRING, "repo_source": STRING, "deployment_id": STRING, "alias": STRING,
                                   "tools": {"type": "array", "items": STRING}, "contract": STRING,
                                   "scope": {"type": "object"}, "expected_revision": {"type": "integer", "minimum": 1}},
                                  ("deployment_id", "alias", "tools"))),
    "unbind_agent_service": ("停用一条绑定并立即撤回其授权。", schema({"binding_id": STRING}, ("binding_id",))),
    "list_agent_bindings": ("列出服务绑定（可按 Agent 或部署过滤）。", schema({"agent_id": STRING, "deployment_id": STRING}, ())),
    "install_repo_resources": ("按 schema_version:2/3 清单长期安装 repo 的 skills/MCP，并启动常驻 MCP。重复安装复用资源。"
                               "传 candidate_id 时复用 prepare_repo_review 固定的 commit 安装该候选，origin/ref 可省略且必须与候选一致。",
                               schema({"origin": STRING, "ref": STRING, "candidate_id": STRING}, ())),
    "prepare_repo_review": ("只读审阅：clone 仓库并把 ref 固定为 commit，返回候选 ID、commit 和 AGENTS/Installation/entrypoint 文档存在状态。"
                            "component 选择同 repo 多组件中的相对目录；不执行安装脚本、不启动业务 MCP、不触发任务；临时检出超过 24 小时自动清理。",
                            schema({"origin": STRING, "ref": STRING, "component": STRING}, ("origin",))),
    "read_repo_review_doc": ("读取候选或已安装仓库的审阅文档（AGENTS.md、Installation.md、installation.md、entrypoint.md、runbook），单文件不超过 128 KiB。"
                             "candidate_id 用于安装前审阅，installation_id 用于安装后复核同一版本，返回 phase 区分阶段。runbook 仅对声明了它的 mcp-service 组件开放。",
                             schema({"candidate_id": STRING, "installation_id": STRING,
                                     "document": {"type": "string", "enum": list(REVIEW_DOCUMENTS) + ["runbook"]}}, ("document",))),
    "list_repo_resources": ("列出 repo 安装、命名空间、skills/MCP 清单和服务进程状态。", schema({})),
    "get_repo_resources": ("查询 repo 资源清单与常驻 MCP 健康状态。", schema({"installation_id": STRING}, ("installation_id",))),
    "start_repo_resources": ("启动已安装 repo 的常驻 MCP，不执行工作。", schema({"installation_id": STRING}, ("installation_id",))),
    "stop_repo_resources": ("停止 repo 常驻 MCP，影响此安装的所有实例；只在用户要求停止服务时使用。", schema({"installation_id": STRING}, ("installation_id",))),
    "register_work": ("注册可重复触发的仓库工作。需先启动 agent-base serve 或 worker 才会调度执行。", schema({
        "name": STRING, "repo": STRING, "task": STRING, "ref": STRING, "model": STRING, "effort": STRING,
        "workspace_source": STRING, "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
        "cron": STRING, "timezone": STRING, "overlap": {"enum": ["skip", "allow"]}}, ("name", "repo"))),
    "trigger_work": ("将已注册工作的一次执行持久化入队，返回 run_id 或跳过原因。key 可用于请求去重。", schema({
        "name": STRING, "key": STRING, "payload": {"type": "object"}}, ("name",))),
    "list_works": ("列出工作定义、cron、下次时间和暂停状态。", schema({})),
    "submit_run": ("在新目录 clone 并安装运行一个仓库。返回后台 run_id，不等待完成。", schema({
        "repo": STRING, "task": STRING, "ref": STRING, "model": STRING, "effort": STRING}, ("repo",))),
    "install_repo": ("只 clone 和安装仓库，不发起 agent 任务，完成后状态为 prepared。", schema({"repo": STRING, "ref": STRING}, ("repo",))),
    "start_run": ("运行已经 prepared 的仓库。每个 prepared run 只允许启动一次。", schema({**RUN, "task": STRING}, ("run_id",))),
    "get_run": ("查询任务状态、结果和产物路径。", schema(RUN, ("run_id",))),
    "list_runs": ("列出最近任务，包括状态和结果。", schema({})),
    "wait_run": ("等待任务最多 20 秒后返回最新状态。仍运行时可以继续等待。", schema({**RUN, "seconds": {"type": "number", "minimum": 0, "maximum": 20}}, ("run_id",))),
    "cancel_run": ("请求取消任务；须等待状态变为 cancelled 才能确认停止。", schema(RUN, ("run_id",))),
    "read_run_log": ("读取任务日志末尾，定位失败。仅允许本次运行目录内的 .log/.jsonl 文件。", schema({**RUN, "file": STRING}, ("run_id", "file"))),
}


def _service_call(service, name: str, args: dict):
    """Shared service management tools for the Admin MCP entry."""
    from .review import Reviews
    from .service_catalog import ServiceCatalog
    from .service_install import ManagedServices
    from .service_store import ServiceStore
    catalog = ServiceCatalog(service.config)
    managed = catalog.managed
    stores = catalog.services
    if name in {"prepare_service_install", "apply_service_install"}:
        reviews = Reviews(service.config)
        candidate = None
        if args.get("candidate_id"):
            candidate = reviews.candidate(args["candidate_id"])
        else:
            if not args.get("origin"):
                raise ValueError("需要 origin 或 candidate_id")
            candidate = reviews.prepare(args["origin"], args.get("ref"), component=args.get("component"))
        plan = managed.plan_install(
            candidate, deployment_id=args.get("deployment_id"), owner=args.get("owner", ""),
            credentials=args.get("credentials"), env=args.get("env"), port=args.get("port"),
            backend_headers=args.get("backend_headers"), auth_mode=args.get("auth_mode"),
            publish=bool(args.get("publish")), allow_guardian_restart=bool(args.get("allow_guardian_restart")))
        if name == "prepare_service_install":
            return {"candidate_id": candidate["id"], "commit": candidate["commit"],
                    "component": candidate.get("component"), "plan": _plan_public(plan)}
        if not plan.get("deployment_id"):
            raise ValueError("安装需要 deployment_id")
        operation = managed.apply_install(plan, idempotency_key=args.get("idempotency_key"),
                                          background=bool(args.get("background", True)))
        return {**operation, "plan": _plan_public(operation.get("plan", {}))}
    if name == "get_service_operation":
        if args.get("operation_id"):
            return managed.get_operation(args["operation_id"], wait=args.get("wait", 0))
        if not args.get("deployment_id"):
            raise ValueError("需要 operation_id 或 deployment_id")
        operations = managed.services.operations(args["deployment_id"], limit=1)
        if not operations:
            raise ValueError("该部署尚无操作记录")
        return managed.get_operation(operations[0]["id"], wait=args.get("wait", 0))
    if name == "cancel_service_operation":
        return managed.cancel_operation(args["operation_id"])
    if name == "publish_service":
        return catalog.publish(args["deployment_id"])
    if name == "withdraw_service":
        return catalog.withdraw(args["deployment_id"], reason=args.get("reason", ""))
    if name == "register_external_service":
        return catalog.register_external(deployment_id=args["deployment_id"], service_id=args["service_id"],
                                         url=args["url"], contract=args["contract"],
                                         expected_tools=args.get("expected_tools"), headers=args.get("headers"),
                                         owner=args.get("owner", ""), validate=bool(args.get("validate", True)))
    if name == "list_services":
        return catalog.list_services()
    if name == "get_service":
        return {**catalog.describe(catalog._deployment(args["deployment_id"])),
                "health": managed.gateway_health(catalog._deployment(args["deployment_id"])),
                "recent_operations": [{k: op[k] for k in ("id", "kind", "status", "error", "created", "finished")}
                                      for op in managed.services.operations(args["deployment_id"], limit=10)],
                "recent_health_events": managed.services.health_events(args["deployment_id"], limit=10)}
    if name == "probe_service":
        from .service_supervisor import probe_deployment
        return probe_deployment(service.config, args["deployment_id"])
    if name == "read_service_log":
        return managed.read_log(args["deployment_id"], args["name"], tail_bytes=args.get("tail_bytes", 16000))
    if name == "list_service_incidents":
        return stores.incidents(deployment_id=args.get("deployment_id"))
    if name == "get_service_incident":
        return stores.incident(args["incident_id"])
    if name == "start_service":
        return managed.start(args["deployment_id"])
    if name == "stop_service":
        return managed.stop(args["deployment_id"], reason="admin-mcp")
    if name == "restart_service":
        return managed.restart(args["deployment_id"], reason="admin-mcp")
    if name == "list_service_agents":
        return stores.agent_identities()
    if name == "prepare_knowledge_migration":
        from .knowledge_migration import preflight
        return preflight(service.config, deployment_id=args.get("deployment_id") or "project-knowledge",
                         owner=args.get("owner", ""), alias=args.get("alias") or "knowledge",
                         publish=bool(args.get("publish", True)), in_place=bool(args.get("in_place", False)))
    if name == "apply_knowledge_migration":
        from .knowledge_migration import apply
        return apply(service.config, args["plan"], idempotency_key=args.get("idempotency_key"))
    if name == "get_knowledge_migration":
        from .knowledge_migration import summary
        return summary(service.config, args.get("deployment_id") or "project-knowledge")
    if name == "verify_knowledge_migration":
        from .knowledge_migration import verify
        return verify(service.config, args.get("deployment_id") or "project-knowledge")
    if name == "rollback_knowledge_migration":
        from .knowledge_migration import rollback
        return rollback(service.config, args.get("deployment_id") or "project-knowledge",
                        reason=args.get("reason", ""))
    if name == "bind_agent_service":
        agent_id = args.get("agent_id")
        if not agent_id and args.get("repo_source"):
            identity = stores.ensure_agent_identity(source=args["repo_source"],
                                                    name=args["repo_source"].removesuffix("repo:")[:40])
            agent_id = identity["id"]
        if not agent_id:
            raise ValueError("需要 agent_id 或 repo_source")
        return catalog.bind(agent_id=agent_id, deployment_id=args["deployment_id"], alias=args["alias"],
                            tools=args["tools"], contract=args.get("contract"), scope=args.get("scope"),
                            expected_revision=args.get("expected_revision"))
    if name == "unbind_agent_service":
        return catalog.unbind(args["binding_id"])
    if name == "list_agent_bindings":
        return stores.bindings(agent_id=args.get("agent_id"), deployment_id=args.get("deployment_id"))
    raise ValueError(f"未知服务管理工具：{name}")


def _plan_public(plan: dict) -> dict:
    """Plan view without credential references or internal progress."""
    return {key: plan.get(key) for key in ("kind", "deployment_id", "service_id", "service_title", "contract",
                                           "origin", "ref", "commit", "component", "manifest_digest", "revision_id",
                                           "reuse_revision", "transport", "owner", "port", "publish",
                                           "allow_guardian_restart", "health", "expected_tools", "runbook",
                                           "restart_safe", "session_sharing", "existing_deployment",
                                           "missing", "risks", "result")}


def admin_call(service: Service, name: str, args: dict):
    service_tools = {"prepare_service_install", "apply_service_install", "get_service_operation",
                     "cancel_service_operation", "publish_service", "withdraw_service",
                     "register_external_service", "list_services", "get_service", "probe_service",
                     "read_service_log", "list_service_incidents", "get_service_incident",
                     "start_service", "stop_service", "restart_service", "list_service_agents",
                     "bind_agent_service", "unbind_agent_service", "list_agent_bindings",
                     "prepare_knowledge_migration", "apply_knowledge_migration",
                     "get_knowledge_migration", "verify_knowledge_migration",
                     "rollback_knowledge_migration"}
    if name in service_tools:
        return _service_call(service, name, args)
    if name in {"install_repo_resources", "list_repo_resources", "get_repo_resources", "start_repo_resources", "stop_repo_resources"}:
        from .repositories import Repositories
        repositories = Repositories(service.config)
        if name == "install_repo_resources":
            from .review import Reviews
            if args.get("candidate_id"):
                reviews = Reviews(service.config)
                candidate = reviews.candidate(args["candidate_id"])
                if args.get("origin") and args["origin"] != candidate["origin"]:
                    raise ValueError("origin 与审阅候选不一致；安装复用候选时不要提供不同的 origin")
                if args.get("ref") is not None and args["ref"] != candidate["ref"]:
                    raise ValueError("ref 与审阅候选不一致；候选已固定 commit，不接受不同 ref")
                item = repositories.install(candidate["origin"], candidate["ref"],
                                            checkout=Path(candidate["root"]) / "checkout", commit=candidate["commit"])
                reviews.consume(args["candidate_id"])
                return item
            if not args.get("origin"):
                raise ValueError("需要 origin 或 candidate_id")
            return repositories.install(args["origin"], args.get("ref"))
        if name == "list_repo_resources":
            return repositories.list()
        installation_id = args["installation_id"]
        if name == "start_repo_resources":
            return repositories.start(installation_id)
        if name == "stop_repo_resources":
            return repositories.stop(installation_id)
        item = repositories.get(installation_id)
        return {**item, "health": repositories.health(item)}
    if name in {"prepare_repo_review", "read_repo_review_doc"}:
        from .review import Reviews
        reviews = Reviews(service.config)
        if name == "prepare_repo_review":
            return reviews.prepare(args["origin"], args.get("ref"), component=args.get("component"))
        if bool(args.get("candidate_id")) == bool(args.get("installation_id")):
            raise ValueError("candidate_id 与 installation_id 必须且只能提供一个")
        installation = None
        if args.get("installation_id"):
            from .repositories import Repositories
            installation = Repositories(service.config).get(args["installation_id"])
        return reviews.read(candidate_id=args.get("candidate_id"), installation=installation, document=args["document"])
    if name in {"register_work", "trigger_work", "list_works"}:
        from .scheduler import Scheduler
        scheduler = Scheduler(service.config)
        if name == "register_work":
            return scheduler.register(**args)
        if name == "trigger_work":
            return scheduler.trigger(**args)
        return scheduler.works()
    if name == "submit_run":
        return public(service.submit(**args))
    if name == "install_repo":
        return public(service.submit(**args, install_only=True))
    if name == "start_run":
        return public(service.start_prepared(args["run_id"], args.get("task")))
    if name == "get_run":
        return public(service.get(args["run_id"]))
    if name == "list_runs":
        return [public(job) for job in service.list()]
    if name == "wait_run":
        return public(service.wait(args["run_id"], args.get("seconds", 20)))
    if name == "cancel_run":
        return public(service.cancel(args["run_id"]))
    if name == "read_run_log":
        job = service.get(args["run_id"])
        file = within(Path(job["root"]), args["file"])
        if file.suffix not in {".log", ".jsonl"} or not file.is_file():
            raise ValueError("仅允许普通 .log/.jsonl 文件")
        with file.open("rb") as stream:
            stream.seek(max(0, file.stat().st_size - 12000))
            text = stream.read().decode("utf-8", errors="replace")
        return {"text": text, "tail": True}
    raise ValueError(f"未知管理工具：{name}")


async def serve(args):
    service = Service(Config.load(args.config)) if args.config else None
    package = Path(args.package).resolve() if args.package else None
    workspace = Path(args.workspace).resolve() if args.workspace else None
    if not service and (not package or not workspace):
        raise ValueError("需要 --config 或 --package + --workspace")
    manifest = json.loads((package / "resolved.json").read_text(encoding="utf-8")) if package else None
    tools = {t["name"]: t for t in manifest.get("tools", {}).get("local", [])} if manifest else {}
    server = Server("agent-base-admin" if service else "agent-base-local")

    @server.list_tools()
    async def list_tools():
        if service:
            return [types.Tool(name=name, description=desc, inputSchema=spec) for name, (desc, spec) in ADMIN_TOOLS.items()]
        return [types.Tool(name=name, description=tool.get("description", name), inputSchema=tool["input_schema"]) for name, tool in tools.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            if service:
                jsonschema.validate(arguments, ADMIN_TOOLS[name][1])
                result = await asyncio.to_thread(admin_call, service, name, arguments)
            else:
                tool = tools[name]
                jsonschema.validate(arguments, tool["input_schema"])
                with tempfile.TemporaryDirectory(prefix="local-tool-", dir=workspace) as temporary:
                    temp = Path(temporary)
                    request = temp / "input.json"
                    request.write_text(json.dumps({"protocol_version": 1, "arguments": arguments,
                        "context": {"workspace": str(workspace), "artifact_dir": str(workspace / "artifacts")}}), encoding="utf-8")
                    env = scrub_environment(os.environ, parse_credential_names(os.environ.get(CREDENTIAL_NAME_MARKER)))
                    cancelled = threading.Event()
                    try:
                        code = await asyncio.to_thread(run_process, expand(tool["argv"], package, workspace, args.python),
                            cwd=workspace, env=env, output=temp / "out.json", error=temp / "err.log", input_file=request,
                            deadline=time.time() + tool.get("timeout_seconds", 60), cancelled=cancelled.is_set, isolate=False)
                    finally:
                        cancelled.set()
                    if code:
                        raise RuntimeError(f"业务工具退出码 {code}：" + (temp / "err.log").read_text(encoding="utf-8")[-2000:])
                    result = json.loads((temp / "out.json").read_text(encoding="utf-8"))
                    if not isinstance(result, dict) or result.get("ok") is False:
                        raise ValueError(f"业务工具返回失败或非对象结果：{str(result)[:1000]}")
                    if tool.get("output_schema"):
                        out_schema = tool["output_schema"]
                        if isinstance(out_schema, str):
                            out_schema = json.loads(within(package, out_schema).read_text(encoding="utf-8"))
                        jsonschema.validate(result, out_schema)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except Exception as error:
            # SDK propagates an actual isError tool result; never fake success.
            raise ValueError(str(error)) from error

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--package")
    parser.add_argument("--workspace")
    parser.add_argument("--python", default=sys.executable)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
