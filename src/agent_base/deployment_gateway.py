"""One gateway process per managed deployment.

stdio backends are held by this gateway and exposed as local Streamable HTTP
MCP; native HTTP backends run on a private port and every request is proxied
through the stable gateway endpoint. Authorization is enforced here, on the
server side: per-run grants, binding liveness and tool scopes are checked
before any business call reaches the backend.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import Config
from .manifests import service_expand
from .processes import alive, identity
from .service_install import gateway_token_path
from .service_store import ServiceStore
from .store import Store

BACKEND_PORT_ATTEMPTS = 5
PROJECT_ARGUMENTS = ("project", "project_id", "project_slug", "projects", "namespace", "namespace_id")


class GrantError(ValueError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class Gateway:
    def __init__(self, config: Config, deployment_id: str, endpoint: str):
        self.config = config
        self.deployment_id = deployment_id
        self.endpoint = endpoint
        self.services = ServiceStore(config.data)
        self.store = Store(config.data)
        self.deployment = self.services.deployment(deployment_id)
        if not self.deployment:
            raise ValueError(f"部署不存在：{deployment_id}")
        self.root = Path(self.deployment_root())
        self.external = self.deployment["mode"] == "external"
        if self.external:
            # External endpoints are reached through a platform-owned loopback
            # access layer; the platform never starts or restarts the remote.
            self.manifest = {"schema_version": 3, "kind": "mcp-service",
                             "runtime": {"transport": "streamable_http", "timeout_seconds": 60},
                             "health": {"startup_timeout_seconds": 30, "timeout_seconds": 10},
                             "operations": {"restart_safe": False}}
            self.transport = "streamable_http"
            self.external_url = (self.deployment["config"].get("external_url")
                                 or self.deployment["config"].get("url")
                                 or self.deployment["endpoint"])
        else:
            self.manifest = self.services.revision(self.deployment["revision_id"])["manifest"]
            self.transport = self.manifest["runtime"]["transport"]
        self.token = gateway_token_path(self.root).read_text().strip()
        self.generation = self.deployment["generation"]
        self.tools: list[str] = []
        self.tool_definitions: list[types.Tool] = []
        self.in_flight = 0
        self.state = {"ready": False, "upstream": "starting", "protocol_ok": False, "last_check": None,
                      "backend": {}, "error": None}
        self.session = None
        self.session_lock = asyncio.Lock()
        self.exit_stack = None
        self.backend_port = None
        self.backend_process = None

    def deployment_root(self) -> str:
        return str(self.config.data / "deployments" / self.deployment_id)

    # -- backend --------------------------------------------------------------
    def backend_environment(self) -> dict:
        env = dict(os.environ)
        for dest, value in self.manifest.get("runtime", {}).get("environment", {}).items():
            env[dest] = self._resolve(value)
        for dest, value in self.deployment["config"].get("env", {}).items():
            if isinstance(value, str):
                env[dest] = value
        for dest, value in self.deployment["config"].get("credentials", {}).items():
            env[dest] = self._resolve(value)
        return env

    @staticmethod
    def _resolve(value) -> str:
        if isinstance(value, dict) and value.get("from_env"):
            source = value["from_env"]
            if not os.environ.get(source):
                raise ValueError(f"缺少业务凭据环境变量：{source}")
            return os.environ[source]
        if isinstance(value, str):
            return value
        raise ValueError("环境配置必须是字符串或 from_env 引用")

    def backend_headers(self) -> dict:
        headers = {}
        for name, value in {**self.deployment["config"].get("headers", {}),
                            **self.deployment["config"].get("backend_headers", {})}.items():
            headers[name] = self._resolve(value)
        return headers

    def _pick_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    async def start_backend(self, stack: AsyncExitStack):
        if self.external:
            await self._start_external()
            return
        revision = self.services.revision(self.deployment["revision_id"])
        source = Path(revision["root"]) / "source"
        data_dir = self.root / "data"
        if self.transport == "stdio":
            argv = service_expand(self.manifest["runtime"]["argv"], package=source,
                                  python=revision["python"], data_dir=data_dir)
            before = _children(os.getpid())
            connection = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=argv[0], args=argv[1:], cwd=source, env=self.backend_environment())))
            created = _children(os.getpid()) - before
            backend = {"pid": None, "birth": None}
            if len(created) == 1:
                backend["pid"], backend["birth"] = created.pop()
            self.session = await stack.enter_async_context(ClientSession(connection[0], connection[1]))
            async with asyncio.timeout(self._startup_timeout()):
                await self.session.initialize()
                await self._load_tools(self.session)
            self.state["backend"] = backend
            self.state.update(ready=True, upstream="ready", protocol_ok=True, last_check=time.time())
        else:
            path = self.manifest["runtime"].get("endpoint_path", "/mcp")
            last_error = None
            for _ in range(BACKEND_PORT_ATTEMPTS):
                port = self._pick_port()
                argv = service_expand(self.manifest["runtime"]["argv"], package=source,
                                      python=revision["python"], data_dir=data_dir, port=port)
                log = (self.root / "logs" / "backend.log").open("ab")
                # Stay in the gateway's process group: killing the gateway tree
                # must take the private-port backend with it, leaving no orphans.
                process = await asyncio.create_subprocess_exec(*argv, cwd=source,
                                                               env=self.backend_environment(),
                                                               stdin=subprocess.DEVNULL,
                                                               stdout=log, stderr=log)
                backend = {"pid": process.pid, "birth": identity(process.pid)}
                self.backend_process = process
                url = f"http://127.0.0.1:{port}{path}"
                try:
                    async with asyncio.timeout(self._startup_timeout()):
                        ready = await self._wait_backend(url)
                    if ready:
                        self.backend_port = port
                        self.state["backend"] = backend
                        self.state.update(ready=True, upstream="ready", protocol_ok=True, last_check=time.time())
                        self.state["backend_url"] = url
                        return
                    last_error = "backend 未在启动宽限内就绪"
                except Exception as error:
                    last_error = str(error)[:300]
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                await asyncio.sleep(0.2)
            raise ValueError(f"原生 HTTP 后端启动失败：{last_error}；见 logs/backend.log")

    async def _start_external(self):
        """Initialize the remote endpoint once and cache its tool contract."""
        url = self.external_url
        headers = self.backend_headers()
        deadline = time.monotonic() + self._startup_timeout()
        last_error = None
        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(headers=headers, timeout=10, trust_env=False) as client:
                    async with streamable_http_client(url, http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            async with asyncio.timeout(10):
                                await session.initialize()
                                await self._load_tools(session)
                self.state["backend"] = {"external": True, "url": url}
                self.state["backend_url"] = url
                self.state.update(ready=True, upstream="ready", protocol_ok=True, last_check=time.time())
                return
            except Exception as error:
                last_error = f"{type(error).__name__}: {str(error)[:300]}"
                await asyncio.sleep(0.5)
        self.state.update(upstream="unavailable", protocol_ok=False,
                          last_check=time.time(), error=last_error)
        raise ValueError(f"外部 MCP 初始化失败：{last_error}")

    def _startup_timeout(self) -> float:
        return max(5, self.manifest.get("health", {}).get("startup_timeout_seconds", 60))

    async def _load_tools(self, session: ClientSession) -> list[str]:
        """Cache the complete upstream tool definitions (schema included)."""
        definitions, cursor = [], None
        while True:
            listing = await session.list_tools(cursor=cursor)
            definitions.extend(listing.tools)
            cursor = listing.nextCursor
            if not cursor:
                break
        self.tool_definitions = definitions
        self.tools = [tool.name for tool in definitions]
        return self.tools

    async def _wait_backend(self, url: str) -> bool:
        """The backend is ready when it completes a real MCP initialize."""
        deadline = time.monotonic() + self._startup_timeout()
        while time.monotonic() < deadline:
            if self.backend_process and self.backend_process.returncode is not None:
                return False
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False,
                                             headers=self.backend_headers()) as client:
                    async with streamable_http_client(url, http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            async with asyncio.timeout(5):
                                await session.initialize()
                                await self._load_tools(session)
                            return True
            except Exception:
                await asyncio.sleep(0.2)
        return False

    # -- authorization --------------------------------------------------------
    def verify_grant(self, request: Request) -> dict:
        """Server-side check: valid grant, live run/turn, enabled binding, scope."""
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        definition = self.services.verify_grant(token)
        if not definition:
            raise GrantError("服务授权无效或已过期", 401)
        if definition.get("kind") == "probe":
            if definition.get("deployment_id") != self.deployment_id:
                raise GrantError("探测授权不适用于本部署", 403)
            return definition
        if definition.get("deployment_id") != self.deployment_id:
            raise GrantError("授权不适用于本部署", 403)
        # Immediate revocation wins over any frozen snapshot.
        binding = self._binding(definition)
        if binding is None:
            raise GrantError("服务绑定已撤销或停用", 403)
        self._check_liveness(definition)
        return definition

    def _binding(self, definition: dict):
        for binding in self.services.bindings(agent_id=definition.get("agent_id"),
                                              deployment_id=self.deployment_id):
            if binding["enabled"] and binding["alias"] == definition.get("alias"):
                return binding
        return None

    def _check_liveness(self, definition: dict):
        run_id = definition.get("run_id")
        if run_id:
            try:
                job = self.store.get(run_id)
            except ValueError as error:
                raise GrantError("所属任务不存在", 403) from error
            if job["cancel_requested"] or job["status"] not in {"preparing", "running", "verifying"}:
                raise GrantError("所属任务已结束或取消", 403)
            if job["deadline"] and time.time() >= job["deadline"]:
                raise GrantError("所属任务已超时", 403)
        turn_id = definition.get("turn_id")
        if turn_id:
            with self.store.connection() as conn:
                row = conn.execute("SELECT status,cancel_requested FROM conversation_turns WHERE id=?",
                                   (turn_id,)).fetchone()
            if not row or row["status"] != "running" or row["cancel_requested"]:
                raise GrantError("所属对话轮次已结束", 403)
        pid, birth = definition.get("pid"), definition.get("birth")
        if pid and not alive(pid, birth):
            raise GrantError("所属运行进程已退出", 403)

    @staticmethod
    def _schema_project_keys(schema: dict) -> set[str]:
        keys = set()
        if not isinstance(schema, dict):
            return keys
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                if name in PROJECT_ARGUMENTS:
                    keys.add(name)
                keys |= Gateway._schema_project_keys(child)
        items = schema.get("items")
        if isinstance(items, dict):
            keys |= Gateway._schema_project_keys(items)
        return keys

    @staticmethod
    def _project_values(arguments: dict) -> list[tuple[str, object]]:
        found = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in PROJECT_ARGUMENTS:
                        found.append((key, item))
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(arguments)
        return found

    def _tool_project_support(self, tool_name: str) -> bool:
        for tool in self.tool_definitions:
            if tool.name == tool_name:
                return bool(self._schema_project_keys(tool.inputSchema or {}))
        return False

    def check_scope(self, definition: dict, tool_name: str, arguments: dict):
        """Enforce the binding's project whitelist on every call.

        Only `projects` is a supported generic scope. A tool that cannot accept
        a project argument cannot be scoped, so a scoped binding rejects the
        call instead of silently querying every project. Nested project
        arguments are inspected as well.
        """
        allowed = (definition.get("scope") or {}).get("projects")
        if not allowed:
            return
        if not isinstance(allowed, list) or not allowed:
            raise GrantError("绑定项目范围结构无效")
        if not self._tool_project_support(tool_name):
            raise GrantError(f"工具 {tool_name} 没有项目参数，无法执行绑定项目范围")
        values = self._project_values(arguments or {})
        if not values:
            raise GrantError("调用缺少项目参数，无法执行绑定项目范围")
        for key, value in values:
            items = value if isinstance(value, list) else [value]
            if not items:
                raise GrantError(f"调用参数 {key} 不能为空，无法执行绑定项目范围")
            for item in items:
                if not isinstance(item, str) or not item or item not in allowed:
                    raise GrantError(f"调用参数 {key} 超出绑定项目范围")

    def check_tool(self, definition: dict, tool: str):
        if definition.get("kind") == "probe":
            probe = self.manifest.get("health", {}).get("probe_tool") or {}
            if tool == probe.get("tool"):
                return
            raise GrantError("探测授权只允许声明的只读探针", 403)
        allowed = definition.get("tools") or []
        if tool not in allowed:
            raise GrantError(f"工具不在授权范围内：{tool}", 403)

    # -- app ------------------------------------------------------------------
    def build_app(self) -> Starlette:
        if self.transport == "stdio":
            server = Server(self.deployment["service_id"])

            @server.list_tools()
            async def list_tools():
                # The catalog matches the actual call scope: probe grants see
                # the full contract, run grants see their binding's tools.
                definition = self.verify_grant(server.request_context.request)
                exposed = self.tool_definitions
                if definition.get("kind") != "probe":
                    allowed = set(definition.get("tools") or [])
                    exposed = [tool for tool in self.tool_definitions if tool.name in allowed]
                return exposed

            @server.call_tool()
            async def call_tool(name: str, arguments: dict):
                definition = self.verify_grant(server.request_context.request)
                self.check_tool(definition, name)
                if definition.get("kind") != "probe":
                    self.check_scope(definition, name, arguments)
                deadline = time.monotonic() + self.manifest["runtime"].get("timeout_seconds", 60)
                self.in_flight += 1
                try:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                        async with self.session_lock:
                            answer = await self.session.call_tool(name, arguments)
                    # Preserve isError, structuredContent, annotations and meta.
                    return answer
                finally:
                    self.in_flight -= 1

            manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

            @asynccontextmanager
            async def lifespan(app):
                async with AsyncExitStack() as stack:
                    self.exit_stack = stack
                    await self.start_backend(stack)
                    watcher = asyncio.create_task(self._watch())
                    async with manager.run():
                        self._write_runtime()
                        try:
                            yield
                        finally:
                            watcher.cancel()
                            await asyncio.gather(watcher, return_exceptions=True)

            class MCPRoute:
                async def __call__(self, scope, receive, send):
                    await manager.handle_request(scope, receive, send)

            app = Starlette(routes=[Route("/health", self.health_route),
                                    Route("/mcp", MCPRoute(), methods=["GET", "POST", "DELETE"])],
                            lifespan=lifespan)
        else:
            @asynccontextmanager
            async def lifespan(app):
                async with AsyncExitStack() as stack:
                    self.exit_stack = stack
                    await self.start_backend(stack)
                    self._write_runtime()
                    yield
                    if self.backend_process and self.backend_process.returncode is None:
                        self.backend_process.terminate()
                        try:
                            await asyncio.wait_for(self.backend_process.wait(), 5)
                        except asyncio.TimeoutError:
                            self.backend_process.kill()

            app = Starlette(routes=[Route("/health", self.health_route),
                                    Route("/mcp", self.proxy_mcp, methods=["GET", "POST", "DELETE"])],
                            lifespan=lifespan)
        return self._guard(app)

    async def _watch(self):
        while True:
            await asyncio.sleep(5)
            if self.external:
                try:
                    async with httpx.AsyncClient(headers=self.backend_headers(), timeout=8,
                                                 trust_env=False) as client:
                        async with streamable_http_client(self.external_url, http_client=client) as streams:
                            async with ClientSession(streams[0], streams[1]) as session:
                                async with asyncio.timeout(8):
                                    await session.initialize()
                    self.state.update(protocol_ok=True, upstream="ready", last_check=time.time())
                except Exception as error:
                    self.state.update(protocol_ok=False, upstream="unavailable",
                                      last_check=time.time(), error=str(error)[:200])
                continue
            try:
                if self.session_lock.locked():
                    continue
                async with asyncio.timeout(3):
                    async with self.session_lock:
                        await self.session.send_ping()
                self.state.update(protocol_ok=True, upstream="ready", last_check=time.time())
            except Exception:
                self.state.update(protocol_ok=False, upstream="unavailable", last_check=time.time())

    async def health_route(self, request: Request):
        return JSONResponse(self.health())

    def health(self) -> dict:
        backend = self.state.get("backend") or {}
        return {"deployment_id": self.deployment_id, "service_id": self.deployment["service_id"],
                "generation": self.generation, "revision_id": self.deployment.get("revision_id"),
                "config_revision": self.deployment.get("config_revision"), "external": self.external,
                "pid": os.getpid(), "birth": identity(os.getpid()),
                "transport": self.transport, "ready": self.state["ready"],
                "backend": {**backend, "port": self.backend_port},
                "backend_alive": alive(backend.get("pid"), backend.get("birth")) if backend.get("pid") else None,
                "tools": self.tools, "in_flight": self.in_flight,
                "upstream": self.state["upstream"], "protocol_ok": self.state["protocol_ok"],
                "last_check": self.state["last_check"], "error": self.state["error"]}

    def _write_runtime(self):
        item = {"generation": self.generation, "gateway_pid": os.getpid(), "gateway_birth": identity(os.getpid()),
                "revision_id": self.deployment.get("revision_id"),
                "config_revision": self.deployment.get("config_revision"),
                "external": self.external,
                "backend": self.state.get("backend") or {}, "backend_port": self.backend_port,
                "transport": self.transport, "tools": self.tools, "started_at": time.time(), "ready": True}
        temporary = self.root / "runtime.json.tmp"
        temporary.write_text(json.dumps(item))
        temporary.replace(self.root / "runtime.json")

    # -- proxy mode -----------------------------------------------------------
    async def proxy_mcp(self, request: Request):
        passthrough = self.deployment["config"].get("auth_mode") == "passthrough"
        try:
            if passthrough:
                if not request.headers.get("authorization"):
                    return JSONResponse({"error": "需要服务凭证"}, status_code=401)
                definition = None
            else:
                definition = self.verify_grant(request)
        except GrantError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)
        body = await request.body()
        listing = False
        message = None
        if definition is not None and request.method == "POST":
            try:
                self._check_proxied_call(definition, body)
            except GrantError as error:
                return self._rpc_error(body, str(error))
            try:
                message = json.loads(body)
            except ValueError:
                message = None
            listing = isinstance(message, dict) and message.get("method") == "tools/list"
        allowed = None
        if listing and definition is not None and definition.get("kind") != "probe":
            # The advertised directory must match the enforced call scope.
            allowed = set(definition.get("tools") or [])
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() in {"content-type", "accept", "mcp-session-id", "last-event-id"}}
        if passthrough:
            headers["Authorization"] = request.headers.get("authorization", "")
        else:
            headers.update(self.backend_headers())
        url = self.state["backend_url"]
        client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=None), trust_env=False)
        self.in_flight += 1

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except httpx.HTTPError as error:
                self.state.update(upstream="unavailable", protocol_ok=False, last_check=time.time(),
                                  error=str(error)[:200])
                yield json.dumps({"error": "upstream unavailable"}).encode()
            finally:
                self.in_flight -= 1
                await upstream.aclose()
                await client.aclose()

        try:
            upstream = await client.send(client.build_request(request.method, url, content=body,
                                                              headers=headers), stream=True)
        except httpx.HTTPError as error:
            self.in_flight -= 1
            await client.aclose()
            self.state.update(upstream="unavailable", protocol_ok=False, last_check=time.time(),
                              error=str(error)[:200])
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)
        if allowed is not None:
            return await self._filtered_listing(upstream, client, allowed)
        # Forward identity-relevant headers; the upstream content type decides
        # JSON vs SSE and the MCP session id must round-trip unchanged.
        forwarded = {k: v for k, v in upstream.headers.items()
                     if k.lower() in {"content-type", "mcp-session-id"}}
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=forwarded)

    async def _filtered_listing(self, upstream, client: httpx.AsyncClient, allowed: set[str]):
        """Buffer a tools/list response and drop tools outside the binding.

        JSON and SSE responses are both handled; an unknown content type is
        rejected instead of leaking the unfiltered directory.
        """
        try:
            raw = await upstream.aread()
            content_type = upstream.headers.get("content-type", "")
            status = upstream.status_code

            def filtered(payload):
                if not isinstance(payload, dict):
                    return payload
                result = payload.get("result")
                if isinstance(result, dict) and isinstance(result.get("tools"), list):
                    payload = {**payload, "result": {**result,
                                                       "tools": [tool for tool in result["tools"]
                                                                 if tool.get("name") in allowed]}}
                return payload

            if "application/json" in content_type:
                try:
                    return JSONResponse(filtered(json.loads(raw)), status_code=status)
                except ValueError:
                    return Response(raw, status_code=status, media_type=content_type)
            if "text/event-stream" in content_type:
                text = raw.decode("utf-8", errors="replace")
                lines = []
                for line in text.splitlines(keepends=True):
                    stripped = line.rstrip("\r\n")
                    if stripped.startswith("data:"):
                        data = stripped[5:].strip()
                        if data and data != "[DONE]":
                            try:
                                data = json.dumps(filtered(json.loads(data)), ensure_ascii=False)
                            except ValueError:
                                pass
                        lines.append("data: " + data + ("\n\n" if line.endswith("\n") else ""))
                    else:
                        lines.append(line)
                return Response("".join(lines), status_code=status, media_type="text/event-stream")
            return self._rpc_error(b"{}", "网关无法按绑定过滤该服务的工具目录（未知响应类型）")
        finally:
            self.in_flight -= 1
            await upstream.aclose()
            await client.aclose()

    def _check_proxied_call(self, definition: dict, body: bytes):
        try:
            message = json.loads(body)
        except ValueError:
            return  # non-JSON bodies are protocol-level; the backend rejects them
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return
        params = message.get("params") or {}
        name = params.get("name")
        if isinstance(name, str):
            self.check_tool(definition, name)
            if definition.get("kind") != "probe":
                self.check_scope(definition, name, params.get("arguments") or {})

    @staticmethod
    def _rpc_error(body: bytes, message: str) -> Response:
        """Rejections surface as protocol-level errors, not HTTP failures."""
        try:
            request = json.loads(body)
            request_id = request.get("id") if isinstance(request, dict) else None
        except ValueError:
            request_id = None
        payload = {"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32603, "message": message[:300]}}
        return JSONResponse(payload, status_code=200, media_type="application/json")

    # -- guard ----------------------------------------------------------------
    def _guard(self, app):
        async def guard(scope, receive, send):
            if scope["type"] == "http":
                request = Request(scope, receive)
                if request.headers.get("origin"):
                    await JSONResponse({"error": "MCP endpoint is not a browser entry"}, status_code=403)(scope, receive, send)
                    return
                if request.url.path == "/health":
                    if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + self.token):
                        await JSONResponse({"error": "需要部署控制凭证"}, status_code=401)(scope, receive, send)
                        return
                elif request.url.path == "/mcp" and request.method != "OPTIONS":
                    try:
                        if self.deployment["config"].get("auth_mode") != "passthrough":
                            self.verify_grant(request)
                    except GrantError as error:
                        # An established MCP session asking for a tool result
                        # gets a protocol-level error, so revocation is visible
                        # to the caller instead of surfacing only on disconnect.
                        body = await request.body()
                        try:
                            message = json.loads(body)
                        except ValueError:
                            message = None
                        if (request.method == "POST" and isinstance(message, dict)
                                and message.get("method") in {"tools/call", "tools/list", "ping"}):
                            response = self._rpc_error(body, str(error))
                        else:
                            response = JSONResponse({"error": str(error)}, status_code=error.status)
                        await response(scope, receive, send)
                        return
            await app(scope, receive, send)
        return guard


def _children(pid: int) -> set:
    try:
        import psutil
        return {(p.pid, p.create_time()) for p in psutil.Process(pid).children()}
    except Exception:
        return set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()
    config = Config.load(args.config)
    services = ServiceStore(config.data)
    deployment = services.deployment(args.deployment)
    if not deployment:
        raise SystemExit(f"部署不存在：{args.deployment}")
    port = deployment["gateway_port"]
    gateway = Gateway(config, args.deployment, f"http://127.0.0.1:{port}/mcp")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as error:
            raise SystemExit(f"网关端口被占用：{port}（{error}）")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        uvicorn.Server(uvicorn.Config(gateway.build_app(), log_level="warning", lifespan="on",
                                      timeout_graceful_shutdown=5)).run(sockets=[sock])


if __name__ == "__main__":
    main()
