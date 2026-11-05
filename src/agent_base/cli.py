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
    commands.add_parser("doctor", help="检查 CLI、凭据是否已配置及版本，不发模型请求")
    lint = commands.add_parser("lint", help="检查 AGENTS.md 和 skill 文档中的绝对路径")
    lint.add_argument("paths", nargs="+", type=Path)
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
        if args.action == "lint":
            from .path_lint import check_prompts
            issues = []
            for path in args.paths:
                if not path.is_dir():
                    raise ValueError(f"目录不存在：{path}")
                issues.extend({"repo": str(path), **item} for item in check_prompts(path))
            emit({"ok": not issues, "issues": issues})
            if issues:
                raise SystemExit(1)
            return
        if args.action == "init":
            emit({"config": str(initialize(Path(args.config), engine=args.engine, command=args.command))})
            return
        config = Config.load(args.config)
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
        if args.action in {"work", "worker"}:
            from .scheduler import Scheduler
            scheduler = Scheduler(config)
            if args.action == "worker":
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
            command = config.runtime["command"]
            found = Path(command[0]).is_file() or bool(shutil.which(command[0]))
            version = None
            if found:
                from .processes import platform_command
                argv, env = platform_command([*command, "--version"], os.environ.copy())
                result = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env)
                version = result.stdout.strip() if result.returncode == 0 else None
            emit({"engine": config.runtime.get("engine", "dsh"), "command_found": found, "cli_version": version,
                  "deepseek_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")), "data": str(config.data),
                  "max_concurrent": config.max_concurrent, "model": config.runtime.get("model"), "online_inference": "not_tested"})
            if not found or not version:
                raise SystemExit(1)
            return
        if args.action == "admin":
            from .admin import Admin
            agent = Admin(config, args.session)
            print(agent.turn(args.message)) if args.message else agent.chat()
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
