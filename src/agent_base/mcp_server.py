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
from .package import expand, within
from .processes import run_process
from .service import Service


def public(job: dict) -> dict:
    return {"installation_id": job["spec"].get("installation_id"), **{key: job.get(key) for key in ("id", "status", "created", "updated", "root", "error", "result", "session_id",
            "work_id", "workspace", "started_at", "finished_at", "deadline", "heartbeat", "session_status")}}


def schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
RUN = {"run_id": STRING}
ADMIN_TOOLS = {
    "install_repo_resources": ("按 schema_version:2 清单长期安装 repo 的 skills/MCP，并启动常驻 MCP。重复安装复用资源。", schema({"origin": STRING, "ref": STRING}, ("origin",))),
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


def admin_call(service: Service, name: str, args: dict):
    if name in {"install_repo_resources", "list_repo_resources", "get_repo_resources", "start_repo_resources", "stop_repo_resources"}:
        from .repositories import Repositories
        repositories = Repositories(service.config)
        if name == "install_repo_resources":
            return repositories.install(**args)
        if name == "list_repo_resources":
            return repositories.list()
        installation_id = args["installation_id"]
        if name == "start_repo_resources":
            return repositories.start(installation_id)
        if name == "stop_repo_resources":
            return repositories.stop(installation_id)
        item = repositories.get(installation_id)
        return {**item, "health": repositories.health(item)}
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
                    env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
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
