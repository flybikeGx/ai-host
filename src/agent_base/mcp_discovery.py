"""MCP discovery: one inventory of every Streamable HTTP MCP the platform serves.

dsh, opencode, Claude Code and plain HTTP clients ask the same two questions:
which MCP endpoints exist right now, and how does a caller connect. The
inventory answers the first; per-agent config fragments plus session grants
answer the second. Credential values never appear in discovery responses:
static tokens are referenced by file path, shared deployments point at the
grant endpoint, and an issued token is returned once, to the requesting caller.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .config import Config

AGENTS = ("dsh", "opencode", "claude-code")
GRANT_MIN_SECONDS = 60
GRANT_MAX_SECONDS = 86400


def _env_name(identifier: str) -> str:
    stem = re.sub(r"[^A-Z0-9]+", "_", identifier.upper()).strip("_") or "SERVER"
    return "AGENT_BASE_MCP_" + stem + "_AUTH"


def _server_name(identifier: str) -> str:
    stem = re.sub(r"[^a-z0-9-.]+", "-", identifier.lower()).strip("-") or "server"
    return "agent-base-" + stem


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    seconds: float = Field(default=3600, ge=GRANT_MIN_SECONDS, le=GRANT_MAX_SECONDS)
    agent_id: str | None = None
    alias: str | None = None


class MCPDiscovery:
    def __init__(self, config: Config):
        self.config = config

    # -- inventory ------------------------------------------------------------
    def inventory(self) -> dict:
        servers = [*self._repo_servers(), *self._deployment_servers(), *self._legacy_knowledge(),
                   *self._info_board()]
        return {"servers": servers, "count": len(servers), "generated_at": time.time()}

    def _info_board(self) -> list[dict]:
        from .info_board_mcp import TOOLS
        from .info_board_service import InfoBoardService
        service = InfoBoardService(self.config)
        if not service.enabled:
            return []
        state = service.status()
        status = {"healthy": "running", "down": "stopped", "disabled": "disabled"}.get(
            state["state"], "unhealthy")
        return [{
            "id": "info-board",
            "name": "共享信息台",
            "kind": "info_board",
            "service_id": "info-board",
            "transport": "streamable_http",
            "url": state.get("endpoint") if status == "running" else None,
            "status": status,
            "stable_endpoint": False,
            "tools": [tool["name"] for tool in TOOLS],
            "auth": {"mode": "platform_grant",
                     "credential": {"type": "platform", "note": "平台任务运行时自动签发短期授权"}},
            "connectable": False,
            "reason": "信息台由平台任务运行时自动挂载；外部客户端暂无独立会话授权入口",
            "usage": {"headers": {"Authorization": "Bearer <运行时短期授权>"},
                      "notes": "人类通过 /info 只读浏览；所有信息来源全局可见"},
        }]

    def _repo_servers(self) -> list[dict]:
        from .repositories import Repositories
        repositories = Repositories(self.config)
        rows = []
        for item in repositories.list():
            if item["status"] != "ready":
                continue
            health = repositories.health(item)
            running = bool(health) and bool(item["endpoint"])
            if not item["enabled"]:
                status = "disabled"
            elif running:
                status = "running"
            else:
                status = "stopped"
            manifest = item["manifest"]
            rows.append({
                "id": item["namespace"],
                "name": manifest.get("package", {}).get("name", item["namespace"]),
                "kind": "repo",
                "installation_id": item["id"],
                "origin": item["origin"],
                "commit": item["commit_sha"][:12],
                "transport": "streamable_http",
                "url": item["endpoint"] if running else None,
                "status": status,
                # The namespace binds a random loopback port on every start.
                "stable_endpoint": False,
                "tools": (health or {}).get("tools", []),
                "skills": (health or {}).get("skills", []),
                "auth": {"mode": "static_token",
                         "credential": {"type": "file",
                                        "path": str(Path(item["root"]) / "token")}},
                "connectable": False,
                "reason": "业务工具调用必须关联属于本安装的活跃 Run（X-Agent-Run 请求头）；"
                          "由平台任务运行时自动挂载，外部 Agent 直接连接只能列出工具目录",
                "usage": {"headers": {"Authorization": "Bearer <installation token>",
                                      "X-Agent-Run": "<活跃 Run ID>"},
                          "notes": "通过 submit_run 提交任务时由平台自动挂载本命名空间"},
            })
        return rows

    def _deployment_servers(self) -> list[dict]:
        from .service_install import ManagedServices
        from .service_store import ServiceStore
        stores = ServiceStore(self.config.data)
        managed = ManagedServices(self.config)
        rows = []
        for deployment in stores.deployments():
            if deployment["desired_state"] == "removed":
                continue
            if managed.is_builtin(deployment):
                rows.append(self._builtin_knowledge_server(stores, deployment))
                continue
            passthrough = deployment["config"].get("auth_mode") == "passthrough"
            health = managed.gateway_health(deployment) or {}
            running = bool(deployment.get("endpoint")) and bool(health.get("gateway_alive")) \
                and bool(health.get("ready"))
            if deployment["desired_state"] == "stopped":
                status = "stopped"
            elif running:
                status = "running"
            else:
                status = "unhealthy"
            published = deployment["publish_state"] == "published"
            connectable = published and running
            reason = None
            if not published:
                reason = "服务未发布或已撤销"
            elif not running:
                reason = "部署当前没有健康活动的服务代际"
            if passthrough:
                auth = {"mode": "passthrough",
                        "credential": {"type": "caller",
                                       "note": "调用方自带服务凭证，网关原样转发 Authorization"}}
                usage_notes = "透传模式：调用方在 Authorization 头携带自己的服务凭证"
            else:
                auth = {"mode": "session_grant",
                        "credential": {"type": "grant", "endpoint": "/api/mcp/grants",
                                       "target": deployment["id"]}}
                usage_notes = ("平台按绑定签发会话授权（POST /api/mcp/grants）；"
                               "工具与项目范围在网关逐调用校验，解绑立即生效")
            bindings = [binding for binding in stores.bindings(deployment_id=deployment["id"])
                        if binding["enabled"]]
            rows.append({
                "id": deployment["id"],
                "name": deployment["config"].get("title") or deployment["service_id"],
                "kind": deployment["mode"],
                "service_id": deployment["service_id"],
                "contract": deployment["config"].get("contract", ""),
                "owner": deployment["config"].get("owner", ""),
                "transport": "streamable_http",
                "url": deployment["endpoint"],
                "upstream_url": deployment["config"].get("external_url")
                if deployment["mode"] == "external" else None,
                "status": status,
                # Gateway ports are fixed per deployment.
                "stable_endpoint": True,
                "tools": deployment["config"].get("tools", []),
                "publish_state": deployment["publish_state"],
                "bindings": [{"agent_id": b["agent_id"], "alias": b["alias"], "tools": b["tools"]}
                             for b in bindings],
                "auth": auth,
                "connectable": connectable,
                "reason": reason,
                "usage": {"headers": {"Authorization": "Bearer <会话授权或调用方凭证>"},
                          "notes": usage_notes},
            })
        return rows

    def _builtin_knowledge_server(self, stores, deployment: dict) -> dict:
        from .knowledge_mcp import TOOLS
        from .knowledge_migration import builtin_root
        from .knowledge_service import KnowledgeService
        state = KnowledgeService(self.config, builtin_root(self.config, deployment)).status()["state"]
        status = {"healthy": "running", "down": "stopped", "disabled": "disabled"}.get(state, "unhealthy")
        published = deployment["publish_state"] == "published"
        bindings = [binding for binding in stores.bindings(deployment_id=deployment["id"])
                    if binding["enabled"]]
        tools = deployment["config"].get("tools") or [tool["name"] for tool in TOOLS]
        connectable = published and status == "running"
        return {
            "id": deployment["id"],
            "name": deployment["config"].get("title") or "知识库",
            "kind": "knowledge",
            "service_id": deployment["service_id"],
            "contract": deployment["config"].get("contract", ""),
            "transport": "streamable_http",
            "url": deployment["endpoint"] if status == "running" else None,
            "status": status,
            "stable_endpoint": False,
            "tools": tools,
            "publish_state": deployment["publish_state"],
            "bindings": [{"agent_id": b["agent_id"], "alias": b["alias"], "tools": b["tools"]}
                         for b in bindings],
            "auth": {"mode": "knowledge_grant",
                     "credential": {"type": "grant", "endpoint": "/api/mcp/grants",
                                    "target": deployment["id"],
                                    "requires": "绑定该部署的 Agent 身份"}},
            "connectable": connectable,
            "reason": None if connectable else
            ("服务未发布" if not published else "知识库当前不可用"),
            "usage": {"headers": {"Authorization": "Bearer <知识库授权>"},
                      "notes": "授权范围跟随该 Agent 的知识库绑定；解绑立即生效"},
        }

    def _legacy_knowledge(self) -> list[dict]:
        from .knowledge_mcp import TOOLS
        from .knowledge_migration import builtin_deployments, builtin_root
        from .knowledge_service import KnowledgeService
        # An in-place migration moved the legacy root under a shared deployment.
        for deployment in builtin_deployments(self.config):
            try:
                if builtin_root(self.config, deployment) == self.config.data / "knowledge":
                    return []
            except ValueError:
                continue
        state = KnowledgeService(self.config).status()
        status = {"healthy": "running", "down": "stopped", "disabled": "disabled"}.get(
            state["state"], "unhealthy")
        return [{
            "id": "knowledge",
            "name": "知识库（平台内置）",
            "kind": "knowledge",
            "transport": "streamable_http",
            "url": state.get("endpoint") if status == "running" else None,
            "status": status,
            "stable_endpoint": False,
            "tools": [tool["name"] for tool in TOOLS],
            "auth": {"mode": "knowledge_grant",
                     "credential": {"type": "grant", "endpoint": "/api/mcp/grants",
                                    "target": "knowledge"}},
            "connectable": status == "running",
            "reason": None if status == "running" else "知识库当前不可用",
            "usage": {"headers": {"Authorization": "Bearer <知识库授权>"},
                      "notes": "POST /api/mcp/grants 签发只读授权；范围见响应 scope"},
        }]

    def well_known(self) -> dict:
        return {"servers": [{"name": _server_name(server["id"]), "url": server["url"],
                             "description": f"{server['name']}（{server['kind']}，{server['status']}）"}
                           for server in self.inventory()["servers"] if server["url"]]}

    # -- per-agent connection configs -----------------------------------------
    def agent_config(self, agent: str) -> dict:
        if agent not in AGENTS:
            raise ValueError(f"不支持的 agent：{agent}；可选 {list(AGENTS)}")
        servers, entries, env = [], {}, {}
        for server in self.inventory()["servers"]:
            entry = {"id": server["id"], "name": _server_name(server["id"]), "kind": server["kind"],
                     "url": server["url"], "status": server["status"], "tools": server["tools"],
                     "connectable": server["connectable"]}
            if not server["connectable"]:
                entry["reason"] = server.get("reason") or "该 MCP 不支持外部直连"
                servers.append(entry)
                continue
            if server["status"] != "running" or not server["url"]:
                entry["reason"] = "服务当前未运行；启动后重新获取配置"
                servers.append(entry)
                continue
            variable = _env_name(server["id"])
            entry["env"] = variable
            entry["credential"] = self._credential(server, variable)
            servers.append(entry)
            env[variable] = entry["credential"]
            entries[entry["name"]] = self._fragment(agent, server, variable)
        return {"agent": agent, "config": self._wrap(agent, entries),
                "env": env, "servers": servers, "instructions": self._instructions(agent)}

    @staticmethod
    def _fragment(agent: str, server: dict, variable: str) -> dict:
        if agent == "opencode":
            return {"type": "remote", "url": server["url"], "oauth": False, "enabled": True,
                    "headers": {"Authorization": "Bearer {env:%s}" % variable}}
        if agent == "claude-code":
            return {"type": "http", "url": server["url"],
                    "headers": {"Authorization": "Bearer ${%s}" % variable}}
        return {"serverName": server["id"], "transport": "streamable-http", "url": server["url"],
                "headers": {"Authorization": "process.env[%s]" % json.dumps(variable)},
                "startupTimeoutMs": 30000, "failOnStartupError": True}

    @staticmethod
    def _wrap(agent: str, entries: dict) -> dict | list:
        if agent == "opencode":
            return {"mcp": entries}
        if agent == "claude-code":
            return {"mcpServers": entries}
        return [{"insert": [{"id": "agent-base-mcp-" + re.sub(r"[^a-z0-9-]+", "-", name.lower()),
                             "name": "@deepseek-ai/dsh-mcp-client", "config": config}
                            for name, config in entries.items()]}]

    @staticmethod
    def _credential(server: dict, variable: str) -> dict:
        credential = server["auth"]["credential"]
        if credential["type"] == "file":
            return {"type": "file", "path": credential["path"],
                    "shell": 'export %s="Bearer $(cat %s)"' % (variable, credential["path"])}
        if credential["type"] == "caller":
            return {"type": "caller", "note": "调用方自带服务凭证，填入 %s" % variable}
        body = {"target": credential["target"], "seconds": 3600}
        if credential.get("requires"):
            body["agent_id"] = "<绑定该服务的 Agent 身份 ID>"
        return {"type": "grant", "method": "POST", "endpoint": credential["endpoint"],
                "body": body, "response_field": "authorization"}

    @staticmethod
    def _instructions(agent: str) -> str:
        if agent == "opencode":
            return ("把 config.mcp 合并进项目 opencode.json 或 ~/.config/opencode/opencode.json；"
                    "请求头通过 {env:VAR} 引用环境变量。会话/知识库授权先 POST /api/mcp/grants，"
                    "把返回的 authorization（含 Bearer 前缀）赋给对应环境变量。")
        if agent == "claude-code":
            return ("把 config.mcpServers 合并进项目 .mcp.json（或 claude mcp add-json）；"
                    "请求头通过 ${VAR} 引用环境变量，启动前必须导出。会话/知识库授权先"
                    " POST /api/mcp/grants，把返回的 authorization 赋给对应环境变量。")
        return ("把 config 中的 insert 行合并进 runtime.patch.yml；headers.Authorization 的值"
                " 需要保持 !!js 表达式 process.env[\"VAR\"] 形式，并确保 dsh 进程环境已设置该变量。"
                "平台任务运行时已自动挂载全部授权 MCP，本片段用于手工接线。")

    # -- session grants ---------------------------------------------------------
    def issue_grant(self, *, target: str, seconds: float = 3600, agent_id: str | None = None,
                    alias: str | None = None) -> dict:
        if not target:
            raise ValueError("需要 target（部署 ID 或 knowledge）")
        seconds = min(max(seconds, GRANT_MIN_SECONDS), GRANT_MAX_SECONDS)
        if target == "knowledge":
            return self._legacy_knowledge_grant(seconds)
        from .service_install import ManagedServices
        from .service_store import ServiceStore
        stores = ServiceStore(self.config.data)
        managed = ManagedServices(self.config)
        deployment = stores.deployment(target)
        if not deployment or deployment["desired_state"] == "removed":
            raise ValueError(f"目标不存在：{target}")
        if managed.is_builtin(deployment):
            return self._builtin_knowledge_grant(stores, deployment, agent_id, alias, seconds)
        if deployment["config"].get("auth_mode") == "passthrough":
            raise ValueError("透传部署由调用方自带凭证，无需平台会话授权")
        if deployment["publish_state"] != "published":
            raise ValueError("只能为已发布的服务部署签发会话授权")
        if deployment["desired_state"] != "running":
            raise ValueError("部署不是运行状态，不能签发会话授权")
        health = managed.gateway_health(deployment) or {}
        if not health.get("gateway_alive") or not health.get("ready"):
            raise ValueError("部署当前没有健康活动的服务代际，不能签发会话授权")
        if not agent_id:
            raise ValueError("共享服务会话授权需要 agent_id（先建立服务绑定）")
        binding = self._select_binding(stores, agent_id, target, alias)
        # No pid/run/turn binding: the grant lives on its TTL, while tool scope,
        # project scope and binding liveness stay enforced on every gateway call.
        definition = {"kind": "call", "agent_id": agent_id, "deployment_id": target,
                      "alias": binding["alias"], "tools": binding["tools"],
                      "binding_revision": binding["revision"], "scope": binding["scope"]}
        token = stores.issue_grant(definition, seconds)
        return {"target": target, "authorization": "Bearer " + token,
                "expires_at": time.time() + seconds, "agent_id": agent_id,
                "alias": binding["alias"], "tools": binding["tools"], "scope": binding["scope"],
                "note": "会话授权只在本次响应中返回一次；解绑或停用绑定立即失效"}

    def _legacy_knowledge_grant(self, seconds: float) -> dict:
        from .knowledge import Knowledge
        from .knowledge_mcp import TOOLS
        from .knowledge_service import KnowledgeService
        status = KnowledgeService(self.config).status()
        if status["state"] != "healthy":
            raise ValueError(f"知识库当前不可用：{status['state']}")
        store = Knowledge(self.config)
        read = [item["id"] for item in store.namespaces()]
        token = store.issue("admin", {"actor": "mcp-discovery", "read_namespace_ids": read,
                                      "submit_namespace_ids": []}, seconds)
        return {"target": "knowledge", "authorization": "Bearer " + token,
                "expires_at": time.time() + seconds,
                "tools": [tool["name"] for tool in TOOLS],
                "scope": {"read_namespace_ids": read, "submit_namespace_ids": []},
                "note": "只读知识库授权，只在本次响应中返回一次"}

    def _builtin_knowledge_grant(self, stores, deployment: dict, agent_id: str | None,
                                 alias: str | None, seconds: float) -> dict:
        from .processes import identity
        from .knowledge_migration import builtin_grant
        if not agent_id:
            raise ValueError("知识库部署会话授权需要 agent_id（先建立服务绑定）")
        binding = self._select_binding(stores, agent_id, deployment["id"], alias)
        token = builtin_grant(self.config, deployment, binding,
                              pid=os.getpid(), birth=identity(os.getpid()),
                              deadline=time.time() + seconds)
        return {"target": deployment["id"], "authorization": "Bearer " + token,
                "expires_at": time.time() + seconds, "agent_id": agent_id,
                "alias": binding["alias"], "tools": binding["tools"], "scope": binding["scope"],
                "note": "知识库授权只在本次响应中返回一次；授权与本平台 Web 服务进程关联，解绑立即失效"}

    @staticmethod
    def _select_binding(stores, agent_id: str, deployment_id: str, alias: str | None) -> dict:
        bindings = [binding for binding in stores.bindings(agent_id=agent_id, deployment_id=deployment_id)
                    if binding["enabled"]]
        if alias:
            bindings = [binding for binding in bindings if binding["alias"] == alias]
        if not bindings:
            raise ValueError(f"Agent {agent_id} 在部署 {deployment_id} 上没有启用的绑定；"
                             "先用 bind_agent_service 创建")
        if len(bindings) > 1:
            raise ValueError("该 Agent 在此部署有多个别名绑定，请指定 alias："
                             + ", ".join(sorted(binding["alias"] for binding in bindings)))
        return bindings[0]


def register(app, config: Config, api):
    discovery = MCPDiscovery(config)

    @app.get("/api/mcp", dependencies=api)
    def mcp_inventory():
        return discovery.inventory()

    @app.get("/api/mcp/config", dependencies=api)
    def mcp_agent_config(agent: str):
        return discovery.agent_config(agent)

    @app.post("/api/mcp/grants", dependencies=api, status_code=201)
    def mcp_grant(body: GrantInput):
        return discovery.issue_grant(target=body.target, seconds=body.seconds,
                                     agent_id=body.agent_id, alias=body.alias)

    @app.get("/.well-known/mcp.json")
    def mcp_well_known():
        return discovery.well_known()
