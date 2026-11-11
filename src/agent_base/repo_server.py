"""One persistent MCP namespace per repo installation, independent of Run lifetimes."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import jsonschema
import uvicorn
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Config
from .package import expand, within
from .processes import run_process
from .repositories import Repositories
from .resource_metrics import direct_children


def env_values(values):
    result = {}
    for key, value in values.items():
        if isinstance(value, dict) and set(value) == {"from_env"}:
            if not os.environ.get(value["from_env"]):
                raise ValueError(f"缺少环境配置：{value['from_env']}")
            result[key] = os.environ[value["from_env"]]
        elif isinstance(value, str):
            result[key] = value
        else:
            raise ValueError("MCP 环境配置须为字符串或 from_env 引用")
    return result


def application(config, item, endpoint):
    repositories = Repositories(config)
    store = repositories.store
    root = Path(item["root"])
    package = root / "source"
    python = (root / "python.txt").read_text()
    manifest = item["manifest"]
    token = (root / "token").read_text().strip()
    server = Server(item["namespace"])
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
    local = {t["name"]: t for t in manifest.get("tools", {}).get("local", [])}
    skills = {s["name"]: s for s in manifest.get("skills", [])}
    upstreams = {}
    remote_tools = {}
    tools = []
    health = {"installation_id": item["id"], "namespace": item["namespace"], "pid": os.getpid(),
              "boot_id": secrets.token_hex(8), "healthy": True, "upstreams": {}, "processes": {}}

    def run_context():
        request = server.request_context.request
        run_id = request.headers.get("x-agent-run") if request else None
        if not run_id:
            raise ValueError("业务调用必须关联 Run")
        job = store.get(run_id)
        if job["spec"].get("installation_id") != item["id"]:
            raise ValueError("Run 不属于本命名空间")
        if job["cancel_requested"] or job["status"] not in {"preparing", "running", "verifying"}:
            raise ValueError("Run 已取消或结束")
        if job["deadline"] and time.time() >= job["deadline"]:
            raise ValueError("Run 已超时")
        return job

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        job = run_context()
        run_id = job["id"]
        started = time.time()
        store.event(run_id, "repo.tool_started", {"namespace": item["namespace"], "tool": name})
        try:
            selected = next((tool for tool in tools if tool.name == name), None)
            if not selected:
                raise ValueError("本命名空间未声明该工具")
            jsonschema.validate(arguments, selected.inputSchema)
            if name == "read_skill":
                skill = skills[arguments["name"]]
                text = within(package, skill["path"] + "/SKILL.md").read_text(encoding="utf-8")
                result = {"name": skill["name"], "path": f".agent/skills/{skill['name']}/SKILL.md", "text": text}
                content = [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            elif name in remote_tools:
                server_id, original = remote_tools[name]
                session, lock, timeout = upstreams[server_id]
                deadline = min(job["deadline"], time.time() + timeout)
                # Serialize a shared stdio session; never change shared cwd/env per Run.
                async with asyncio.timeout(max(0.001, deadline - time.time())):
                    async with lock:
                        answer = await session.call_tool(original, arguments)
                if answer.isError:
                    store.event(run_id, "repo.tool_failed", {"tool": name, "error_kind": "upstream_tool_error"})
                    return answer
                content = answer.content
            else:
                tool = local[name]
                workspace = Path(job["workspace"])
                with tempfile.TemporaryDirectory(prefix="repo-tool-", dir=workspace) as temporary:
                    temp = Path(temporary)
                    request_file = temp / "request.json"
                    request_file.write_text(json.dumps({"protocol_version": 1, "arguments": arguments,
                        "context": {"workspace": str(workspace), "artifact_dir": str(workspace / "artifacts"),
                                    "namespace": item["namespace"], "installation_id": item["id"]}}, ensure_ascii=False))
                    cancelled = threading.Event()
                    def is_cancelled():
                        current = store.get(run_id)
                        return cancelled.is_set() or current["cancel_requested"] or current["status"] not in {"preparing", "running", "verifying"}
                    env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
                    try:
                        code = await asyncio.to_thread(run_process, expand(tool["argv"], package, workspace, python), cwd=workspace, env=env,
                            input_file=request_file, output=temp / "output.json", error=temp / "stderr.log",
                            deadline=min(job["deadline"], time.time() + tool.get("timeout_seconds", 60)), cancelled=is_cancelled)
                    finally:
                        cancelled.set()
                    if code:
                        raise ValueError(f"业务工具退出码 {code}：" + (temp / "stderr.log").read_text()[-1000:])
                    result = json.loads((temp / "output.json").read_text())
                    if not isinstance(result, dict) or result.get("ok") is False:
                        raise ValueError("业务工具返回失败")
                    if tool.get("output_schema"):
                        jsonschema.validate(result, tool["output_schema"])
                    content = [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            store.event(run_id, "repo.tool_finished", {"tool": name, "duration": time.time() - started})
            return content
        except Exception as error:
            kind = "timeout_outcome_unknown" if isinstance(error, TimeoutError) else "tool_error"
            store.event(run_id, "repo.tool_failed", {"tool": name, "error_kind": kind, "message": str(error)[:500]})
            # One failed/slow business call must not restart a shared service.
            # Independent pings determine connection health.
            raise ValueError(f"{kind}: {str(error)[:500] or '调用超时，未自动重试'}") from error

    async def watch():
        while True:
            await asyncio.sleep(5)
            for server_id, (session, lock, _) in upstreams.items():
                try:
                    # Do not mistake a busy serialized backend for a dead one.
                    if lock.locked():
                        continue
                    async with asyncio.timeout(3):
                        async with lock:
                            await session.send_ping()
                except Exception:
                    health["healthy"] = False
                    health["upstreams"][server_id] = "unavailable"

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for specification in manifest.get("mcp", {}).get("servers", []):
                server_id = specification["id"]
                if specification["transport"] == "stdio":
                    before = direct_children(os.getpid())
                    argv = expand(specification["argv"], package, package, python)
                    connection = await stack.enter_async_context(stdio_client(StdioServerParameters(command=argv[0], args=argv[1:],
                        cwd=package, env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]), **env_values(specification.get("environment", {}))})))
                    created = direct_children(os.getpid()) - before
                    if len(created) == 1:
                        pid, birth = created.pop()
                        health["processes"][server_id] = {"pid": pid, "birth": birth}
                else:
                    client = await stack.enter_async_context(httpx.AsyncClient(headers=env_values(specification.get("headers", {})), timeout=30, trust_env=False))
                    connection = await stack.enter_async_context(streamable_http_client(specification["url"], http_client=client))
                session = await stack.enter_async_context(ClientSession(connection[0], connection[1]))
                async with asyncio.timeout(20):
                    await session.initialize()
                    cursor = None
                    while True:
                        listing = await session.list_tools(cursor=cursor)
                        for tool in listing.tools:
                            exposed = server_id + "__" + tool.name
                            remote_tools[exposed] = (server_id, tool.name)
                            tools.append(tool.model_copy(update={"name": exposed}))
                        cursor = listing.nextCursor
                        if not cursor:
                            break
                upstreams[server_id] = (session, asyncio.Lock(), specification.get("timeout_seconds", 60))
                health["upstreams"][server_id] = "ready"
            for name, tool in local.items():
                tools.append(types.Tool(name=name, description=tool.get("description", name), inputSchema=tool["input_schema"]))
            if skills:
                tools.append(types.Tool(name="read_skill", description="按需读取本 repo 固定安装的技能说明。", inputSchema={
                    "type": "object", "properties": {"name": {"type": "string", "enum": list(skills)}}, "required": ["name"], "additionalProperties": False}))
            if len({t.name for t in tools}) != len(tools):
                raise ValueError("资源工具名称冲突，请调整本地工具名或 MCP id")
            watcher = asyncio.create_task(watch())
            async with manager.run():
                ready = root / "service.json"
                temporary = root / "service.json.tmp"
                temporary.write_text(json.dumps({"endpoint": endpoint}))
                temporary.replace(ready)
                try:
                    yield
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)

    async def health_route(request):
        return JSONResponse({**health, "tools": [t.name for t in tools], "skills": list(skills)})

    class MCPRoute:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    app = Starlette(routes=[Route("/health", health_route), Route("/mcp", MCPRoute(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)

    class Guard:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                request = Request(scope)
                if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
                    return await JSONResponse({"error": "repo namespace token required"}, status_code=401)(scope, receive, send)
                if request.headers.get("origin"):
                    return await JSONResponse({"error": "MCP endpoint is not a browser entry"}, status_code=403)(scope, receive, send)
            await app(scope, receive, send)
    return Guard()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--installation", required=True)
    args = parser.parse_args()
    config = Config.load(args.config)
    item = Repositories(config).get(args.installation)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        app = application(config, item, endpoint)
        uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=5)).run(sockets=[sock])


if __name__ == "__main__":
    main()
