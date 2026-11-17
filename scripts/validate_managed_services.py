"""One isolated local acceptance run for phase 2 managed MCP services.

Covers the Admin chain (review → plan → install → validate → publish → bind),
two Agents reusing one deployment, the native HTTP example, a subscription that
survives a restart, deterministic fault recovery, the guardian running through
its real restricted-tool entry, and the isolated knowledge migration. The model
path uses a local OpenAI-compatible gateway and the repository's real dsh
installation when available; otherwise the controlled fake runtime is used and
the receipt says so.

Everything runs under a fresh temporary state directory with fictional
credentials. The script stops the processes it started and never touches an
existing deployment. The JSON receipt is written under `.agent-base/validations`
unless `--receipt` points elsewhere.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROJECT = Path(__file__).resolve().parents[1]
DSH_BIN = PROJECT / ".runtime" / "dsh" / "node_modules" / ".bin" / ("dsh.cmd" if os.name == "nt" else "dsh")
FAKE_DSH = PROJECT / "tests" / "fixtures" / "fake_dsh.py"
TOOLS = ["ticket_search", "ticket_subscribe"]


def log(event: str, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def git_init(path: Path, message: str):
    for command in (["git", "init", "--quiet", str(path)],
                    ["git", "-C", str(path), "add", "."],
                    ["git", "-C", str(path), "-c", "user.name=Agent Base Validation",
                     "-c", "user.email=validation@example.invalid",
                     "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", message]):
        subprocess.run(command, check=True)


def make_repo(path: Path, source: Path | None, files: dict[str, str] | None, message: str):
    if source is not None:
        shutil.copytree(source, path, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    else:
        path.mkdir(parents=True)
        for name, text in (files or {}).items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    git_init(path, message)
    return path


def agent_repo(path: Path, name: str, permissions: list[str], *, fixture_runtime: bool) -> Path:
    token = "FIXTURE_SERVICES\n" if fixture_runtime else ""
    return make_repo(path, None, {
        "AGENTS.md": f"你是 {name}。只通过本 repo 声明的共享 tickets 服务完成工单工作，"
                     "不编造工具结果，不修改工单状态。产物和引用使用相对路径。\n",
        "entrypoint.md": token + "调用共享 tickets 服务的 ticket_search 查询包含「支付」的工单；"
                         + ("获授权时登记一次订阅；" if "ticket_subscribe" in permissions else "")
                         + "最后按统一汇报契约输出。\n",
        "agent.yaml": yaml.safe_dump({
            "schema_version": 3, "kind": "agent",
            "package": {"name": name, "version": "0.1.0"},
            "task": {"default": "使用 tickets 服务查询工单并汇报。"},
            "services": [{"alias": "tickets", "service": "ticket-center",
                          "contract": "ticket-center/v1", "permissions": permissions,
                          "required": True}],
        }, allow_unicode=True, sort_keys=False),
    }, "validation agent")


class AgentGateway:
    """OpenAI-compatible local gateway: subscribe → search → final report."""

    def __init__(self):
        self.requests: list[dict] = []
        self.models: list[str] = []
        self.issues: list[str] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                gateway.requests.append(body)
                gateway.models.append(body.get("model"))
                try:
                    payload = gateway.response(body)
                except Exception as error:  # surfaced in run logs
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(error).encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in payload:
                    self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @staticmethod
    def chunk(delta: dict, finish: str | None = None):
        return {"id": "chatcmpl-validation", "object": "chat.completion.chunk", "created": 1,
                "model": "internal-flash", "choices": [{"index": 0, "delta": delta,
                                                        "finish_reason": finish}]}

    def response(self, body: dict):
        tools = [item["function"]["name"] for item in body.get("tools", [])]
        tool_messages = [message for message in body.get("messages", []) if message.get("role") == "tool"]
        joined = "\n".join(
            message.get("content") if isinstance(message.get("content"), str)
            else json.dumps(message.get("content"), ensure_ascii=False)
            for message in tool_messages)
        search = next((name for name in tools if name.endswith("ticket_search")), None)
        subscribe = next((name for name in tools if name.endswith("ticket_subscribe")), None)
        if not search:
            self.issues.append("model request did not expose ticket_search")
            return [self.chunk({"role": "assistant", "content": ""}, "stop")]
        if subscribe and "subscription_id" not in joined and '"duplicate": true' not in joined:
            return [self.chunk({"role": "assistant", "content": ""}),
                    self.chunk({"tool_calls": [{"index": 0, "id": "call-subscribe", "type": "function",
                                                "function": {"name": subscribe, "arguments": json.dumps(
                                                    {"agent": "ops-agent", "project": "payments"})}}]}),
                    self.chunk({}, "tool_calls")]
        if '"auth_ok": true' not in joined:
            return [self.chunk({"role": "assistant", "content": ""}),
                    self.chunk({"tool_calls": [{"index": 0, "id": "call-search", "type": "function",
                                                "function": {"name": search, "arguments": json.dumps(
                                                    {"query": "支付"})}}]}),
                    self.chunk({}, "tool_calls")]
        report = {"schema_version": 1, "status": "completed",
                  "summary": "shared tickets service roundtrip completed",
                  "artifacts": [], "checks": [{"name": "tool result returned", "status": "passed"}],
                  "issues": [], "next_steps": []}
        return [self.chunk({"role": "assistant", "content": json.dumps(report, ensure_ascii=False)}),
                self.chunk({}, "stop")]

    def boot_ids(self, since: int) -> list[str]:
        values = []
        for request in self.requests[since:]:
            for message in request.get("messages", []):
                if message.get("role") != "tool":
                    continue
                text = str(message.get("content")).replace('\\"', '"')
                match = re.search(r'"boot_id"\s*:\s*"([0-9a-f]+)"', text)
                if match:
                    values.append(match.group(1))
        return sorted(set(values))

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def write_config(path: Path, command: list[str], *, gateway: AgentGateway | None,
                 credential_env: str = "VALIDATION_GATEWAY_KEY") -> Path:
    text = ('[storage]\nroot = "data"\n[queue]\nmax_concurrent = 2\n[runtime]\n'
            f'engine = "dsh"\ncommand = {json.dumps(command)}\n'
            + (f'provider = "validation-gateway"\nmodel = "internal-flash"\neffort = "high"\n'
               f'timeout_seconds = 240\npermission_mode = "workspace-write"\napproval_policy = "never"\n\n'
               f'[providers.validation-gateway]\napi = "openai-completions"\nbase_url = "{gateway.url}"\n'
               f'api_key_env = "{credential_env}"\n\n'
               '[[providers.validation-gateway.models]]\nid = "internal-flash"\ncontext_window = 131072\n'
               '[providers.validation-gateway.models.reasoning_efforts]\nhigh = "high"\n'
               if gateway is not None else 'model = "fixture-model"\neffort = "high"\ntimeout_seconds = 60\n'))
    path.write_text(text, encoding="utf-8")
    return path


def mcp_calls(endpoint: str, token: str, calls: list[tuple[str, dict]]):
    async def run():
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as client:
            async with streamable_http_client(endpoint, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return [await session.call_tool(name, arguments) for name, arguments in calls]
    return asyncio.run(run())


def wait_run(service, run_id: str, seconds: float = 240) -> dict:
    until = time.time() + seconds
    while time.time() < until:
        final = service.get(run_id)
        if final["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
            return final
        time.sleep(0.5)
    return service.get(run_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 managed-service local acceptance")
    parser.add_argument("--receipt", default=None)
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args()
    started = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    state = Path(tempfile.mkdtemp(prefix="agent-base-managed-services-"))
    result: dict = {"kind": "phase-2-managed-services", "started_at": started,
                    "environment": {"platform": sys.platform, "python": sys.version.split()[0],
                                    "real_dsh": DSH_BIN.is_file()},
                    "checks": [], "skipped": [], "services": {}, "agents": {}, "calls": {},
                    "fault_injection": {}, "guardian": {}, "knowledge": {}, "cleanup": {}}
    gateway = AgentGateway() if DSH_BIN.is_file() else None
    os.environ["TICKET_SERVICE_AUTH"] = "validation-ticket-secret"
    os.environ["TICKET_SERVICE_HTTP_AUTH"] = "Bearer validation-http-secret"
    os.environ["VALIDATION_GATEWAY_KEY"] = "validation-fake-gateway-secret"

    def check(name: str, condition: bool, detail=None):
        result["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")
        log("check", name=name)

    try:
        from agent_base.config import Config
        from agent_base.knowledge import Knowledge
        from agent_base.knowledge_migration import apply as knowledge_apply
        from agent_base.knowledge_migration import preflight as knowledge_preflight
        from agent_base.knowledge_migration import verify as knowledge_verify
        from agent_base.processes import alive, identity, kill_tree
        from agent_base.repositories import Repositories
        from agent_base.review import Reviews
        from agent_base.service import Service
        from agent_base.service_catalog import ServiceCatalog
        from agent_base.service_install import ManagedServices
        from agent_base.service_store import ServiceStore
        from agent_base.service_supervisor import ServiceSupervisor

        config_file = state / "config.toml"
        write_config(config_file, [str(DSH_BIN)] if gateway else [sys.executable, str(FAKE_DSH)],
                     gateway=gateway)
        config = Config.load(config_file)
        if gateway is None:
            result["skipped"].append("本地 .runtime/dsh 不可用：真实 dsh + 模型网关链路未验收；"
                                     "Agent 调用由 fake runtime 经真实 MCP 完成")
        log("config", data=str(config.data), real_dsh=bool(gateway))

        repos = state / "repos"
        stdio_repo = make_repo(repos / "ticket-center-stdio",
                               PROJECT / "examples/managed-services/ticket-center-stdio", None,
                               "validation stdio service")
        http_repo = make_repo(repos / "ticket-center-http",
                              PROJECT / "examples/managed-services/ticket-center-http", None,
                              "validation http service")
        triage_repo = agent_repo(repos / "triage-agent", "triage-agent", ["ticket_search"],
                                 fixture_runtime=gateway is None)
        ops_repo = agent_repo(repos / "ops-agent", "ops-agent", ["ticket_search", "ticket_subscribe"],
                             fixture_runtime=gateway is None)

        # 1. Admin chain: pinned review → plan → install → private validation.
        managed = ManagedServices(config)
        reviews = Reviews(config)
        stdio_plan = managed.plan_install(reviews.prepare(str(stdio_repo), None),
                                          deployment_id="ticket-stdio", owner="validation",
                                          allow_guardian_restart=True)
        stdio_operation = managed.apply_install(stdio_plan, background=False)
        check("stdio 托管安装与私有验证", stdio_operation["status"] == "succeeded", stdio_operation.get("error"))
        stdio = ServiceStore(config.data).deployment("ticket-stdio")
        check("stdio 真实 MCP 业务验证", stdio["verification"] == "business_verified", stdio["verification"])

        http_plan = managed.plan_install(
            reviews.prepare(str(http_repo), None), deployment_id="ticket-http", owner="validation",
            credentials={"TICKET_HTTP_AUTH": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}},
            backend_headers={"Authorization": {"from_env": "TICKET_SERVICE_HTTP_AUTH"}})
        http_operation = managed.apply_install(http_plan, background=False)
        check("HTTP 托管安装与私有端口路由", http_operation["status"] == "succeeded", http_operation.get("error"))
        http = ServiceStore(config.data).deployment("ticket-http")
        check("HTTP 真实 MCP 协议/业务探针", http["verification"] == "business_verified", http["verification"])

        catalog = ServiceCatalog(config)
        catalog.publish("ticket-stdio")
        catalog.publish("ticket-http")
        check("发布不等于授权", not ServiceStore(config.data).bindings(deployment_id="ticket-stdio"))

        # 2. Two v3 Agents bind the same deployment with different tool scopes.
        repository_manager = Repositories(config)
        triage_install = repository_manager.install(str(triage_repo))
        ops_install = repository_manager.install(str(ops_repo))
        stores = ServiceStore(config.data)
        triage = stores.agent_identity_for_source("repo:" + triage_install["repo_id"])
        ops = stores.agent_identity_for_source("repo:" + ops_install["repo_id"])
        triage_binding = catalog.bind(agent_id=triage["id"], deployment_id="ticket-stdio", alias="tickets",
                                      tools=["ticket_search"])
        ops_binding = catalog.bind(agent_id=ops["id"], deployment_id="ticket-stdio", alias="tickets",
                                   tools=TOOLS)
        # The same Agent can hold a separate explicit binding to the HTTP deployment.
        http_binding = catalog.bind(agent_id=triage["id"], deployment_id="ticket-http", alias="tickets-http",
                                    tools=["ticket_search"])
        result["agents"] = {
            "triage": {"installation_id": triage_install["id"], "identity": triage["id"],
                       "tools": triage_binding["tools"]},
            "ops": {"installation_id": ops_install["id"], "identity": ops["id"],
                    "tools": ops_binding["tools"]}}
        result["services"] = {name: {"deployment_id": item["id"], "service_id": item["service_id"],
                                     "revision_id": item["revision_id"], "generation": item["generation"],
                                     "verification": item["verification"], "endpoint": item["endpoint"]}
                              for name, item in (("stdio", stdio), ("http", http))}

        # 3. Actual Agent runs: real dsh + local gateway, or the controlled fake runtime.
        service = Service(config)
        run_records = {}
        # Both Agents run concurrently against the one shared deployment.
        submitted_all = {label: service.submit(str(repo))
                         for label, repo in (("triage", triage_repo), ("ops", ops_repo))}
        for label in ("triage", "ops"):
            submitted = submitted_all[label]
            final = wait_run(service, submitted["id"])
            log("run", agent=label, run_id=submitted["id"], status=final["status"])
            check(f"{label} Agent 运行成功", final["status"] == "succeeded",
                  final.get("error") or final["status"])
            run_root = Path(final["root"])
            patch_text = (run_root / "runtime.patch.yml").read_text(encoding="utf-8")
            check(f"{label} 通过统一解析器挂载共享服务",
                  "shared-service-tickets" in patch_text
                  and stdio["endpoint"] in patch_text, "shared-service-tickets")
            events = [json.loads(line) for line in
                      (run_root / "runtime.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            calls = [event for event in events if event.get("type") == "tool_call"]
            completed = [event for event in events if event.get("type") == "tool_result"
                         and event.get("status") == "completed"]
            tools = sorted({call["tool"].rsplit("__", 1)[-1] for call in calls if "__" in call["tool"]})
            boot_ids = set()
            for event in completed:
                text = str(event.get("result", "")).replace('\\"', '"')
                match = re.search(r'"boot_id"\s*:\s*"([0-9a-f]+)"', text)
                if match:
                    boot_ids.add(match.group(1))
            check(f"{label} Agent 实际调用共享服务工具", calls and len(completed) == len(calls),
                  {"calls": [call["tool"] for call in calls], "completed": len(completed)})
            run_records[label] = {"run_id": submitted["id"], "status": final["status"],
                                  "tools": tools, "boot_ids": sorted(boot_ids),
                                  "model": gateway.models[-1] if gateway else "fake-runtime",
                                  "tool_calls": [call["tool"] for call in calls]}
        result["calls"] = run_records
        all_boots = {boot for record in run_records.values() for boot in record.get("boot_ids", [])}
        check("两个 Agent 共享同一活动服务代际", len(all_boots) == 1, sorted(all_boots))
        triage_tools = run_records["triage"].get("tools", [])
        ops_tools = run_records["ops"].get("tools", [])
        check("两个 Agent 的工具范围按绑定隔离",
              "ticket_subscribe" not in triage_tools and "ticket_subscribe" in ops_tools,
              {"triage": triage_tools, "ops": ops_tools})

        # 4. A real grant through the native HTTP route (not a health-only check).
        grant = stores.issue_grant({"kind": "call", "agent_id": triage["id"], "deployment_id": "ticket-http",
                                    "alias": "tickets-http", "tools": ["ticket_search"],
                                    "pid": os.getpid(), "birth": identity(os.getpid())}, 120)

        async def http_scope():
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + grant}, trust_env=False) as client:
                async with streamable_http_client(http["endpoint"], http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = [tool.name for tool in (await session.list_tools()).tools]
                        answer = await session.call_tool("ticket_search", {"query": "支付"})
                        try:
                            denied = await session.call_tool("ticket_subscribe", {"agent": "a", "project": "p"})
                            rejected = denied.isError or "授权" in str(denied.content)
                        except BaseException as error:
                            rejected = "授权" in repr(error)
                        return tools, answer, rejected

        tools, http_answer, rejected = asyncio.run(http_scope())
        http_payload = json.loads(http_answer.content[0].text)
        check("HTTP 稳定路由真实调用", http_payload["ok"] and http_payload["auth_ok"], http_payload)
        check("HTTP 工具目录与绑定范围一致", tools == ["ticket_search"], tools)
        check("HTTP 越权工具被拒绝", rejected, rejected)

        # 5. Subscription persistence across a controlled restart.
        subscriptions = Path(managed.deployment_root("ticket-stdio") / "data" / "subscriptions.jsonl")
        check("订阅登记写入部署持久数据", subscriptions.is_file() and subscriptions.read_text().strip(),
              str(subscriptions))
        before_generation = stores.deployment("ticket-stdio")["generation"]
        managed.stop("ticket-stdio", reason="validation-restart")
        restarted = managed.start("ticket-stdio", reason="validation-restart")
        check("重启产生新代际但保留数据", restarted["generation"] == before_generation + 1
              and subscriptions.is_file() and subscriptions.read_text().strip(),
              {"generation": restarted["generation"]})
        result["persistence"] = {"generation_before": before_generation,
                                 "generation_after": restarted["generation"],
                                 "subscriptions": len([line for line in subscriptions.read_text().splitlines() if line.strip()])}

        # 6. Deterministic fault injection and bounded recovery.
        supervisor = ServiceSupervisor(config)
        deployment = stores.deployment("ticket-stdio")
        kill_tree(deployment["gateway_pid"], deployment["gateway_birth"])
        wait_until = time.time() + 5
        while alive(deployment["gateway_pid"], deployment["gateway_birth"]) and time.time() < wait_until:
            time.sleep(0.05)
        states = [supervisor.probe("ticket-stdio", business=False)["state"] for _ in range(3)]
        check("连续失败进入 unhealthy", states == ["degraded", "degraded", "unhealthy"], states)
        recovered = stores.deployment("ticket-stdio")
        check("监督器按预算恢复一个代际", recovered["generation"] == deployment["generation"] + 1
              and recovered["recovery_attempts"] == 1,
              {"generation": recovered["generation"], "attempts": recovered["recovery_attempts"]})
        incident = stores.incidents(deployment_id="ticket-stdio")[0]
        check("故障单归并", incident["status"] == "open", incident["id"])
        result["fault_injection"] = {"states": states, "generation_before": deployment["generation"],
                                     "generation_after": recovered["generation"],
                                     "recovery_attempts": recovered["recovery_attempts"],
                                     "incident_id": incident["id"], "incident_status": incident["status"]}

        # 7. Guardian through its real runtime entry and restricted tool whitelist.
        from agent_base import guardian as guardian_module
        from agent_base.guardian import run_guardian_patrol
        controlled = Config(path=config.path, data=config.data,
                            runtime={**config.runtime, "command": [sys.executable, str(FAKE_DSH)],
                                     "timeout_seconds": 120},
                            max_concurrent=2,
                            guardian={"enabled": True, "interval_seconds": 300, "timeout_seconds": 180})
        original = guardian_module.GUARDIAN_INSTRUCTIONS
        guardian_module.GUARDIAN_INSTRUCTIONS = (
            "FIXTURE_GUARDIAN\nFIXTURE_GUARDIAN_DEPLOYMENT:ticket-stdio\nFIXTURE_GUARDIAN_RESTART\n" + original)
        # Controlled clock: the recovery backoff has elapsed before patrol.
        stores.update_deployment("ticket-stdio", next_retry_at=0)
        try:
            patrol = run_guardian_patrol(controlled, trigger="validation", deployment_id="ticket-stdio",
                                        incident_id=incident["id"])
        finally:
            guardian_module.GUARDIAN_INSTRUCTIONS = original
        actions_file = config.data / "guardian" / "workspace" / "captured-guardian.json"
        actions = json.loads(actions_file.read_text(encoding="utf-8"))
        after_patrol = stores.deployment("ticket-stdio")
        check("看护 Agent 经真实入口执行受限工具", patrol.get("exit_code") == 0 and actions.get("overview_deployments", 0) >= 1,
              {k: patrol.get(k) for k in ("exit_code", "error")})
        check("看护在策略内执行真实重启", actions.get("restart", {}).get("ok") is True
              and after_patrol["generation"] > recovered["generation"],
              {"actions": actions, "generation": after_patrol["generation"]})
        for _ in range(3):
            supervisor.probe("ticket-stdio", business=False)
        closed = stores.incidents(deployment_id="ticket-stdio")[0]
        check("持续健康后关闭故障单", closed["status"] == "closed" and closed["close_evidence"], closed["status"])
        result["guardian"] = {"runtime": "controlled fake dsh (tool authorization and restart are real)",
                              "patrol": {k: patrol.get(k) for k in ("trigger", "exit_code", "error")},
                              "actions": actions, "generation_after": after_patrol["generation"],
                              "incident_status": closed["status"]}

        # 8. Isolated knowledge takeover with before/after data verification.
        knowledge = Knowledge(config)
        namespace = knowledge.save_namespace({"slug": "validation-project", "name": "验收项目",
                                              "description": "隔离迁移验收"})
        knowledge.bind("admin", {"primary_namespace_id": namespace["id"],
                                 "read_namespace_ids": [namespace["id"]],
                                 "submit_namespace_ids": [namespace["id"]]})
        document = knowledge.publish(namespace["id"], {"title": "验收规则", "summary": "隔离迁移",
                                                       "body_markdown": "# 结算\n每日零点结算。",
                                                       "tags": ["验收"]})
        knowledge.submit(knowledge.binding("admin"), namespace["id"], "create",
                         {"title": "待审", "summary": "待审", "body_markdown": "# 待审\n内容"},
                         "隔离迁移验收", "validation-submission")
        plan = knowledge_preflight(config, deployment_id="project-knowledge", owner="validation")
        knowledge_operation = knowledge_apply(config, plan)
        check("知识库隔离迁移成功", knowledge_operation["status"] == "succeeded",
              knowledge_operation.get("error"))
        migrated = stores.deployment("project-knowledge")
        verification = knowledge_verify(config)
        check("知识库迁移数据与监督归属核对", verification["ok"], verification["checks"])
        check("知识库发布与显式绑定", migrated["publish_state"] == "published"
              and stores.bindings(deployment_id="project-knowledge"),
              {"publish_state": migrated["publish_state"]})
        knowledge_receipt = json.loads(
            Path(knowledge_operation["plan"]["result"]["receipt"]).read_text(encoding="utf-8"))
        result["knowledge"] = {"source_digest": knowledge_receipt["source"]["summary"]["digest"],
                               "target_digest": knowledge_receipt["target"]["summary"]["digest"],
                               "verification": verification["ok"],
                               "generation": migrated["generation"],
                               "publish_state": migrated["publish_state"],
                               "supervision": verification["checks"][1],
                               "document_id": document["id"]}
        # The isolated migration does not touch a legacy production instance.
        from agent_base.knowledge_service import KnowledgeService
        check("未接管既有知识库进程", KnowledgeService(config).status()["state"] in {"down", "disabled"},
              KnowledgeService(config).status()["state"])
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        log("error", message=result["error"])
    finally:
        stopped = []
        try:
            from agent_base.service_install import ManagedServices
            manager = ManagedServices(config)
            for name in ("ticket-stdio", "ticket-http", "project-knowledge"):
                try:
                    manager.stop(name, reason="validation-teardown", drain=False)
                    stopped.append(name)
                except (OSError, ValueError):
                    pass
        except Exception:
            pass
        try:
            # Repo installations own persistent MCP gateways; stop the ones this
            # isolated state directory created before deleting it.
            from agent_base.repositories import Repositories
            repositories = Repositories(config)
            for item in repositories.list():
                try:
                    repositories.stop(item["id"])
                    stopped.append("repo:" + item["id"])
                except (OSError, ValueError):
                    pass
        except Exception:
            pass
        if gateway:
            gateway.stop()
        result["cleanup"] = {"stopped": stopped, "gateway_stopped": bool(gateway),
                             "state_kept": bool(args.keep_state), "state_dir": str(state)}
        result["duration_seconds"] = round(time.time() - started, 1)
        result["ok"] = not result.get("error") and all(item["ok"] for item in result["checks"])
        result["skipped"] = result.get("skipped", [])
        receipt = Path(args.receipt) if args.receipt else (
            PROJECT / ".agent-base" / "validations" / ("managed-services-" + stamp) / "result.json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.keep_state:
            shutil.rmtree(state, ignore_errors=True)
        log("receipt", path=str(receipt), ok=result["ok"], checks=len(result["checks"]))
        print(json.dumps({"ok": result["ok"], "receipt": str(receipt),
                          "checks": len(result["checks"]), "skipped": result["skipped"]},
                         ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
