from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import Config, PROJECT, initialize


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(prog="agent-base", description="仓库 Agent 基座：dsh 原生循环 + Python 任务管理")
    parser.add_argument("--config", default=os.environ.get("AGENT_BASE_CONFIG", str(PROJECT / "agent-base.toml")))
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="创建配置，保留已有配置")
    init.add_argument("--engine", choices=["dsh", "codeagent"], default="dsh")
    init.add_argument("--command")
    commands.add_parser("doctor", help="检查 CLI、当前 provider 与凭据状态及版本，不发模型请求")
    opening = commands.add_parser("open", help="建立一次性本机管理连接并打开主页，不输出凭据")
    opening.add_argument('--port', type=int, default=8765)
    knowledge = commands.add_parser('knowledge', help='管理集中式 Knowledge MCP')
    knowledge.add_argument('knowledge_action', choices=['status','start','stop','restart'])
    info = commands.add_parser('infoboard', help='管理共享信息台 MCP')
    info.add_argument('infoboard_action', choices=['status','start','stop','restart'])
    control = commands.add_parser('control', help='治理面：域/用户/角色/RBAC/审计控制面')
    control_actions = control.add_subparsers(dest='control_action', required=True)
    control_init = control_actions.add_parser('init', help='引导：种子内置角色 + 创建 admin 平台管理员')
    control_init.add_argument('--admin-password', help='初始 admin 密码；缺省时自动生成并在首次创建时输出')
    for control_name in ('users', 'roles', 'domains', 'permissions', 'audit', 'services'):
        control_actions.add_parser(control_name)
    control_check = control_actions.add_parser('service-check', help='探测已注册服务健康并落库')
    control_check.add_argument('service_id')
    repo = commands.add_parser("repo", help="管理 repo 长期安装、skills 和常驻 MCP")
    repo_actions = repo.add_subparsers(dest="repo_action", required=True)
    repo_install = repo_actions.add_parser("install")
    repo_install.add_argument("origin")
    repo_install.add_argument("--ref")
    repo_actions.add_parser("list")
    for name in ("status", "start", "stop", "restart"):
        sub = repo_actions.add_parser(name)
        sub.add_argument("installation_id")
    admin = commands.add_parser("admin", help="启动 admin-agent 对话")
    admin.add_argument("--session", default="default")
    admin.add_argument("--message", help="只发送一轮，保留同名会话上下文")
    guardian = commands.add_parser("guardian", help="运行或查看共享服务看护 Agent")
    guardian_commands = guardian.add_subparsers(dest="guardian_action")
    guardian_run = guardian_commands.add_parser("run", help="立即执行一次巡检（真实运行时入口）")
    guardian_run.add_argument("--deployment", help="重点关注一个部署")
    guardian_run.add_argument("--incident", help="重点处理一张故障单")
    guardian_run.add_argument("--wait", action="store_true", help="前台等待巡检完成")
    guardian_commands.add_parser("status", help="查看看护与监督器状态")
    service = commands.add_parser("service", help="管理共享 MCP 服务部署、发布与绑定")
    service_commands = service.add_subparsers(dest="service_action", required=True)
    for name in ("list", "agents"):
        service_commands.add_parser(name)
    for name in ("get", "probe", "start", "stop", "restart", "publish", "withdraw", "logs", "operations"):
        sub = service_commands.add_parser(name)
        sub.add_argument("deployment_id")
        if name == "logs":
            sub.add_argument("log_name")
        if name == "operations":
            sub.add_argument("--wait", type=float, default=0)
    service_install = service_commands.add_parser("install")
    service_install.add_argument("origin")
    service_install.add_argument("--ref")
    service_install.add_argument("--component")
    service_install.add_argument("deployment_id")
    service_install.add_argument("--owner", default="")
    service_install.add_argument("--publish", action="store_true")
    service_install.add_argument("--wait", action="store_true")
    service_bind = service_commands.add_parser("bind")
    service_bind.add_argument("deployment_id")
    service_bind.add_argument("alias")
    service_bind.add_argument("tools", help="逗号分隔的工具名")
    service_bind.add_argument("--agent", help="Agent 身份 ID")
    service_bind.add_argument("--repo-source", help="repo:<repo_id> 来源映射")
    service_bind.add_argument("--contract")
    service_commands.add_parser("bindings")
    mcp = commands.add_parser("mcp", help="MCP 发现：平台全部 MCP 清单与各 Agent 接入配置")
    mcp_commands = mcp.add_subparsers(dest="mcp_action", required=True)
    mcp_commands.add_parser("list")
    mcp_config = mcp_commands.add_parser("config")
    mcp_config.add_argument("--agent", default="opencode", choices=["dsh", "opencode", "claude-code"])
    register_external = service_commands.add_parser("register-external")
    register_external.add_argument("deployment_id")
    register_external.add_argument("service_id")
    register_external.add_argument("url")
    register_external.add_argument("contract")
    register_external.add_argument("--tools", default="", help="逗号分隔的预期工具名")
    register_external.add_argument("--header-env", action="append", default=[],
                                   help="NAME=ENV_VAR 形式的受控请求头")
    register_external.add_argument("--owner", default="")
    for name in ("run", "install"):
        item = commands.add_parser(name)
        item.add_argument("repo")
        item.add_argument("--task")
        item.add_argument("--ref")
        item.add_argument("--model")
        item.add_argument("--effort")
        item.add_argument("--wait", action="store_true")
    start = commands.add_parser("start")
    start.add_argument("run_id")
    start.add_argument("--task")
    chat_turn = commands.add_parser("_chat_execute", help=argparse.SUPPRESS)
    chat_turn.add_argument("turn_id")
    oss_mock = commands.add_parser("oss-mock", help="启动本地模拟 OSS 统一登录服务（I-4 验证）")
    oss_mock.add_argument("--port", type=int, default=8890)
    mcp_mock = commands.add_parser("mcp-mock", help="启动本地模拟 MCP 服务器（网关验证）")
    mcp_mock.add_argument("--port", type=int, default=8891)
    knowledge_mock = commands.add_parser("knowledge-mock", help="启动本地模拟知识库 MCP 服务（网关验证）")
    knowledge_mock.add_argument("--port", type=int, default=8892)
    serve = commands.add_parser("serve", help="启动本地 Web API/UI 和调度器")
    serve.add_argument("--port", type=int, default=8765)
    worker = commands.add_parser("worker", help="运行持久化队列和 cron 调度，无 Web")
    worker.add_argument("--once", action="store_true")
    work = commands.add_parser("work", help="管理可重复触发的工作")
    work_commands = work.add_subparsers(dest="work_action", required=True)
    register = work_commands.add_parser("register")
    register.add_argument("name")
    register.add_argument("repo")
    for option in ("task", "ref", "model", "effort", "workspace-source", "cron"):
        register.add_argument("--" + option)
    register.add_argument("--timeout-seconds", type=float, default=1800)
    register.add_argument("--timezone", default="Asia/Shanghai")
    register.add_argument("--overlap", choices=["skip", "allow"], default="skip")
    work_commands.add_parser("list")
    for action in ("trigger", "pause", "resume", "triggers"):
        sub = work_commands.add_parser(action)
        sub.add_argument("name")
        if action == "trigger":
            sub.add_argument("--key")
            sub.add_argument("--payload", default="{}")
    commands.add_parser("list")
    for name in ("status", "cancel", "wait", "events", "_execute"):
        item = commands.add_parser(name)
        item.add_argument("run_id")
        if name == "wait":
            item.add_argument("--seconds", type=float, default=30)
        if name == "events":
            item.add_argument("--after", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.action == "init":
            emit({"config": str(initialize(Path(args.config), engine=args.engine, command=args.command))})
            return
        config = Config.load(args.config)
        if args.action == 'open':
            import webbrowser
            from .knowledge import Knowledge
            from .knowledge_web import admin_secret
            store = Knowledge(config)
            admin_secret(store)
            ticket = store.issue('bootstrap', {'actor':'local-admin'}, 120)
            opened = webbrowser.open(f'http://127.0.0.1:{args.port}/knowledge#connect={ticket}')
            emit({'opened': bool(opened), 'homepage': f'http://127.0.0.1:{args.port}/', 'connection': '一次性连接，有效期 120 秒，不输出凭据'})
            return
        if args.action == 'knowledge':
            from .knowledge_service import KnowledgeService
            service = KnowledgeService(config)
            if args.knowledge_action in {'stop','restart'}:
                result = service.stop()
            if args.knowledge_action in {'start','restart'}:
                result = service.ensure(explicit=True)
            if args.knowledge_action == 'status':
                result = service.status()
            emit(result)
            return
        if args.action == 'infoboard':
            from .info_board_service import InfoBoardService
            service = InfoBoardService(config)
            if args.infoboard_action in {'stop','restart'}:
                result = service.stop()
            if args.infoboard_action in {'start','restart'}:
                result = service.ensure(explicit=True)
            if args.infoboard_action == 'status':
                result = service.status()
            emit(result)
            return
        if args.action == 'control':
            import secrets
            from .governance.service import Governance
            gov = Governance(config)
            if args.control_action == 'init':
                password = args.admin_password or os.environ.get('AGENT_BASE_ADMIN_PASSWORD')
                generated = False
                if password is None:
                    password = secrets.token_urlsafe(12)
                    generated = True
                result = gov.bootstrap(admin_password=password)
                if result['admin_created'] and generated:
                    result['admin_password'] = password
                    result['note'] = '初始密码仅本次输出，请妥善保存后可通过 Web 修改'
                emit(result)
            elif args.control_action == 'users':
                emit(gov.list_users())
            elif args.control_action == 'roles':
                emit(gov.list_roles())
            elif args.control_action == 'domains':
                emit(gov.list_domains())
            elif args.control_action == 'permissions':
                emit(gov.list_permissions())
            elif args.control_action == 'audit':
                emit(gov.list_audit())
            elif args.control_action == 'services':
                from .governance.registry import ServiceRegistry
                emit(ServiceRegistry(gov.store).list())
            elif args.control_action == 'service-check':
                from .governance.registry import ServiceRegistry
                emit(ServiceRegistry(gov.store).check(args.service_id))
            return
        if args.action == "_chat_execute":
            from .conversations import Conversations
            Conversations(config).execute(args.turn_id)
            return
        if args.action == "repo":
            from .repositories import Repositories
            repositories = Repositories(config)
            if args.repo_action == "install":
                emit(repositories.install(args.origin, args.ref))
            elif args.repo_action == "list":
                emit(repositories.list())
            elif args.repo_action == "status":
                item = repositories.get(args.installation_id)
                emit({**item, "health": repositories.health(item)})
            else:
                if args.repo_action in {"stop", "restart"}:
                    item = repositories.stop(args.installation_id)
                if args.repo_action in {"start", "restart"}:
                    item = repositories.start(args.installation_id)
                emit(item)
            return
        if args.action == "serve":
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(config), host="127.0.0.1", port=args.port)
            return
        if args.action == "oss-mock":
            import uvicorn
            from .mocks.oss import create_app
            uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
            return
        if args.action == "mcp-mock":
            import uvicorn
            from .mocks.mcp import echo_http_app
            uvicorn.run(echo_http_app(), host="127.0.0.1", port=args.port)
            return
        if args.action == "knowledge-mock":
            import uvicorn
            from .mocks.knowledge import knowledge_http_app
            uvicorn.run(knowledge_http_app(), host="127.0.0.1", port=args.port)
            return
        if args.action in {"work", "worker"}:
            from .scheduler import Scheduler
            scheduler = Scheduler(config)
            if args.action == "worker":
                import logging
                from .knowledge_service import KnowledgeService
                from .info_board_service import InfoBoardService
                try:
                    KnowledgeService(config).ensure()
                except ValueError:
                    logging.exception('Knowledge MCP unavailable')
                try:
                    if InfoBoardService(config).enabled:
                        InfoBoardService(config).ensure()
                except ValueError:
                    logging.exception('Info Board MCP unavailable')
                if args.once:
                    emit(scheduler.tick())
                else:
                    import signal
                    signal.signal(signal.SIGTERM, lambda *_: scheduler.stop_event.set())
                    signal.signal(signal.SIGINT, lambda *_: scheduler.stop_event.set())
                    scheduler.run()
                return
            if args.work_action == "register":
                emit(scheduler.register(**{k: v for k, v in vars(args).items() if k not in {"config", "action", "work_action"}}))
            elif args.work_action == "list":
                emit(scheduler.works())
            elif args.work_action == "trigger":
                emit(scheduler.trigger(args.name, key=args.key, payload=json.loads(args.payload)))
            elif args.work_action == "triggers":
                emit(scheduler.triggers(args.name))
            else:
                emit(scheduler.enable(args.name, args.work_action == "resume"))
            return
        if args.action == "doctor":
            from .engines import provider_credential_env
            command = config.runtime["command"]
            found = Path(command[0]).is_file() or bool(shutil.which(command[0]))
            version = None
            if found:
                from .processes import platform_command
                argv, env = platform_command([*command, "--version"], os.environ.copy())
                result = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env)
                version = result.stdout.strip() if result.returncode == 0 else None
            provider = config.runtime.get("provider", "deepseek-official")
            credential_env = provider_credential_env(config.runtime)
            emit({"engine": config.runtime.get("engine", "dsh"), "command_found": found, "cli_version": version,
                  "provider": provider, "credential_env": credential_env,
                  "credential_configured": bool(os.environ.get(credential_env)) if credential_env else None,
                  "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
                  "data": str(config.data),
                  "max_concurrent": config.max_concurrent, "model": config.runtime.get("model"),
                  "online_inference": "not_tested"})
            if not found or not version:
                raise SystemExit(1)
            if credential_env and not os.environ.get(credential_env):
                raise SystemExit(1)
            return
        if args.action == "admin":
            from .admin import Admin
            agent = Admin(config, args.session)
            print(agent.turn(args.message)) if args.message else agent.chat()
            return
        if args.action == "guardian":
            from .guardian import guardian_status, run_guardian_patrol
            if args.guardian_action == "status":
                emit(guardian_status(config))
            else:
                if not args.wait:
                    emit({"note": "前台执行一次巡检；持续看护请在配置 [guardian] enabled 后运行 serve/worker"})
                emit(run_guardian_patrol(config, trigger="manual", deployment_id=args.deployment,
                                         incident_id=args.incident))
            return
        if args.action == "service":
            from .service_catalog import ServiceCatalog
            from .service_install import ManagedServices
            from .service_supervisor import probe_deployment
            from .service_store import ServiceStore
            catalog = ServiceCatalog(config)
            managed = ManagedServices(config)
            if args.service_action == "list":
                emit(catalog.list_services())
            elif args.service_action == "agents":
                emit(ServiceStore(config.data).agent_identities())
            elif args.service_action == "get":
                emit({**catalog.describe(catalog._deployment(args.deployment_id)),
                      "health": managed.gateway_health(catalog._deployment(args.deployment_id))})
            elif args.service_action == "probe":
                emit(probe_deployment(config, args.deployment_id))
            elif args.service_action == "start":
                emit(managed.start(args.deployment_id))
            elif args.service_action == "stop":
                emit(managed.stop(args.deployment_id, reason="cli"))
            elif args.service_action == "restart":
                emit(managed.restart(args.deployment_id, reason="cli"))
            elif args.service_action == "publish":
                emit(catalog.publish(args.deployment_id))
            elif args.service_action == "withdraw":
                emit(catalog.withdraw(args.deployment_id))
            elif args.service_action == "logs":
                emit(managed.read_log(args.deployment_id, args.log_name))
            elif args.service_action == "operations":
                operations = managed.services.operations(args.deployment_id)
                if args.wait and operations:
                    emit(managed.get_operation(operations[0]["id"], wait=args.wait))
                else:
                    emit(operations)
            elif args.service_action == "install":
                from .review import Reviews
                candidate = Reviews(config).prepare(args.origin, args.ref, component=args.component)
                plan = managed.plan_install(candidate, deployment_id=args.deployment_id, owner=args.owner,
                                            publish=args.publish)
                operation = managed.apply_install(plan, background=not args.wait,
                                                  idempotency_key="cli-" + plan["revision_id"])
                if args.wait:
                    operation = managed.get_operation(operation["id"], wait=300)
                emit(operation)
            elif args.service_action == "bind":
                stores = ServiceStore(config.data)
                agent_id = args.agent
                if not agent_id and args.repo_source:
                    agent_id = stores.ensure_agent_identity(source=args.repo_source,
                                                            name=args.repo_source.removesuffix("repo:")[:40])["id"]
                if not agent_id:
                    raise ValueError("需要 --agent 或 --repo-source")
                emit(catalog.bind(agent_id=agent_id, deployment_id=args.deployment_id, alias=args.alias,
                                  tools=[t for t in args.tools.split(",") if t.strip()],
                                  contract=args.contract))
            elif args.service_action == "bindings":
                emit(ServiceStore(config.data).bindings())
            elif args.service_action == "register-external":
                headers = {}
                for item in args.header_env:
                    name, _, source = item.partition("=")
                    headers[name] = {"from_env": source}
                emit(catalog.register_external(deployment_id=args.deployment_id, service_id=args.service_id,
                                               url=args.url, contract=args.contract,
                                               expected_tools=[t for t in args.tools.split(",") if t.strip()],
                                               headers=headers, owner=args.owner))
            return
        if args.action == "mcp":
            from .mcp_discovery import MCPDiscovery
            discovery = MCPDiscovery(config)
            if args.mcp_action == "list":
                emit(discovery.inventory())
            else:
                emit(discovery.agent_config(args.agent))
            return
        from .service import Service
        from .mcp_server import public
        service = Service(config)
        if args.action in {"run", "install"}:
            job = service.submit(args.repo, args.task, ref=args.ref, model=args.model, effort=args.effort, install_only=args.action == "install")
            if args.wait:
                job = service.wait(job["id"], seconds=300)
            emit(public(job))
            if args.wait and job["status"] in {"failed", "interrupted", "cancelled", "timed_out"}:
                raise SystemExit(1)
        elif args.action == "start":
            emit(public(service.start_prepared(args.run_id, args.task)))
        elif args.action == "list":
            emit([public(job) for job in service.list()])
        elif args.action == "status":
            emit(public(service.get(args.run_id)))
        elif args.action == "cancel":
            emit(public(service.cancel(args.run_id)))
        elif args.action == "wait":
            emit(public(service.wait(args.run_id, args.seconds)))
        elif args.action == "events":
            emit(service.store.events(args.run_id, args.after))
        elif args.action == "_execute":
            service.execute(args.run_id)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"错误：{error}\n")


if __name__ == "__main__":
    main()
