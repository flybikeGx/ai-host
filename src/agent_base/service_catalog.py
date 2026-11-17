"""Service catalog: publication, external registration and explicit bindings.

Publication only registers a verified deployment in the platform directory; it
never auto-authorizes agents. Bindings are the single source of granted tools
and data scope, checked again server-side by each deployment gateway.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlparse

import httpx

from .config import Config
from .service_install import ManagedServices
from .service_store import ServiceStore


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class ServiceCatalog:
    def __init__(self, config: Config):
        self.config = config
        self.services = ServiceStore(config.data)
        self.managed = ManagedServices(config)

    # -- B1: publication ------------------------------------------------------
    def _deployment(self, deployment_id: str) -> dict:
        deployment = self.services.deployment(deployment_id)
        if not deployment:
            raise ValueError(f"部署不存在：{deployment_id}")
        return deployment

    def publish(self, deployment_id: str, *, actor: str = "admin") -> dict:
        deployment = self._deployment(deployment_id)
        if deployment["publish_state"] == "published":
            return {**deployment, "already_published": True}
        if deployment["publish_state"] == "withdrawn":
            raise ValueError("已撤销发布的服务不能直接重新发布；请重新验证后再发布")
        if deployment["desired_state"] == "removed":
            raise ValueError("部署已标记删除")
        if deployment["verification"] not in {"protocol_verified", "business_verified"}:
            raise ValueError("部署尚未通过协议验证，不能发布")
        if deployment["mode"] in {"managed", "external"}:
            health = self.managed.gateway_health(deployment)
            if not health or not health.get("gateway_alive") or not health.get("ready"):
                raise ValueError("部署当前没有健康活动的服务代际，不能发布")
        self.services.update_deployment(deployment_id, publish_state="published")
        self.services.save_service(deployment["service_id"], title=deployment["config"].get("title", ""))
        self.services.create_operation(deployment_id=deployment_id, kind="publish",
                                       plan={"actor": actor, "result": {"publish_state": "published"}},
                                       status="succeeded")
        return self._deployment(deployment_id)

    def withdraw(self, deployment_id: str, *, actor: str = "admin", reason: str = "") -> dict:
        deployment = self._deployment(deployment_id)
        if deployment["publish_state"] != "withdrawn":
            self.services.update_deployment(deployment_id, publish_state="withdrawn")
        # Withdrawal blocks new access immediately: drop outstanding grants.
        self.services.revoke_grants(deployment_id=deployment_id)
        deployment = self._deployment(deployment_id)
        if self.managed.is_builtin(deployment):
            from .knowledge_migration import revoke_builtin_grants
            for binding in self.services.bindings(deployment_id=deployment_id):
                revoke_builtin_grants(self.config, deployment, binding["agent_id"])
        self.services.create_operation(deployment_id=deployment_id, kind="withdraw",
                                       plan={"actor": actor, "reason": reason,
                                             "result": {"publish_state": "withdrawn"}},
                                       status="succeeded")
        return self._deployment(deployment_id)

    # -- B2: external registration -------------------------------------------
    def register_external(self, *, deployment_id: str, service_id: str, url: str,
                          contract: str, expected_tools: list[str] | None = None,
                          headers: dict | None = None, owner: str = "",
                          validate: bool = True) -> dict:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("外部服务 URL 必须是 http(s) 端点")
        parsed = urlparse(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("外部服务 URL 不得包含凭据、查询或片段")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", service_id or ""):
            raise ValueError("service_id 必须是小写字母、数字和连字符")
        if self.services.deployment(deployment_id):
            raise ValueError(f"部署已存在：{deployment_id}")
        headers = headers or {}
        for name, value in headers.items():
            if not isinstance(value, dict) or set(value) != {"from_env"}:
                raise ValueError(f"外部服务请求头 {name} 只支持 from_env 引用")
        expected_tools = expected_tools or []
        config = {"owner": owner, "headers": headers, "expected_tools": expected_tools,
                  "contract": contract, "auth_mode": "external", "url": url, "external_url": url,
                  "health": {"interval_seconds": 60, "timeout_seconds": 10}}
        self.services.create_deployment(deployment_id, service_id=service_id, mode="external",
                                        config=config, endpoint=None)
        # The platform owns a loopback access layer; the remote process is never
        # started, stopped or restarted by the platform.
        self.services.update_deployment(deployment_id, desired_state="running")
        operation = self.services.create_operation(deployment_id=deployment_id, kind="register_external",
                                                   plan={"url": url, "contract": contract, "owner": owner})
        if validate:
            result = self.validate_external(deployment_id)
            if not result["ok"]:
                self.services.update_operation(operation["id"], status="failed",
                                               error=f"外部服务协议验证失败：{result['errors']}")
                try:
                    self.managed.stop(deployment_id, reason="register-failed", drain=False, desired="stopped")
                except (OSError, ValueError):
                    pass
                raise ValueError(f"外部服务协议验证失败：{result['errors']}")
            self.services.update_operation(operation["id"], status="succeeded",
                                           plan={"url": url, "contract": contract, "owner": owner,
                                                 "result": {"verification": result["verification"],
                                                            "tools": result["tools"]}})
        return self._deployment(deployment_id)

    def validate_external(self, deployment_id: str) -> dict:
        """Validate through the platform access layer, never a bare HTTP check."""
        deployment = self._deployment(deployment_id)
        if deployment["mode"] != "external":
            raise ValueError("该部署不是外部服务")
        try:
            deployment = self.managed.ensure_external_gateway(deployment_id, reason="external-validate")
        except ValueError as error:
            result = {"ok": False, "errors": [str(error)[:300]],
                      "verification": "not_verified", "tools": []}
        else:
            result = self.managed.validate(deployment_id, business=False)
        if result["ok"]:
            config = {**deployment["config"], "tools": result["tools"]}
            self.services.update_deployment(deployment_id, config=config, verification=result["verification"],
                                            observed_state="healthy", observed_at=time.time(),
                                            observed_evidence=_encoded(result))
        else:
            self.services.update_deployment(deployment_id, observed_state="unhealthy",
                                            observed_at=time.time(), observed_evidence=_encoded(result))
        return result

    def _probe_external_sync(self, endpoint: str, headers: dict, expected_tools: list[str]) -> dict:
        return asyncio.run(self._probe_external(endpoint, headers, expected_tools))

    @staticmethod
    async def _probe_external(endpoint: str, headers: dict, expected_tools: list[str]) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        errors, tools = [], []
        try:
            async with httpx.AsyncClient(headers=headers, timeout=10, trust_env=False) as client:
                async with streamable_http_client(endpoint, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        async with asyncio.timeout(20):
                            await session.initialize()
                            cursor = None
                            while True:
                                listing = await session.list_tools(cursor=cursor)
                                tools.extend(tool.name for tool in listing.tools)
                                cursor = listing.nextCursor
                                if not cursor:
                                    break
        except Exception as error:
            return {"ok": False, "errors": [f"{type(error).__name__}: {str(error)[:200]}"],
                    "verification": "not_verified", "tools": []}
        missing = [name for name in expected_tools if name not in tools]
        if missing:
            errors.append(f"缺少预期工具：{missing}")
        return {"ok": not errors, "errors": errors,
                "verification": "protocol_verified" if not errors else "not_verified",
                "tools": sorted(set(tools))}

    # -- B3: bindings ---------------------------------------------------------
    def deployment_contract(self, deployment: dict) -> str:
        if deployment["mode"] == "managed" and deployment.get("revision_id"):
            revision = self.services.revision(deployment["revision_id"])
            if revision:
                return revision["manifest"].get("service", {}).get("contract", "")
        return deployment["config"].get("contract", "")

    def bind(self, *, agent_id: str, deployment_id: str, alias: str, tools: list[str],
             contract: str | None = None, scope: dict | None = None,
             expected_revision: int | None = None) -> dict:
        deployment = self._deployment(deployment_id)
        if not self.services.agent_identity(agent_id):
            raise ValueError(f"Agent 身份不存在：{agent_id}")
        if not self.managed.is_builtin(deployment):
            scope = scope or {}
            unknown = set(scope) - {"projects"}
            if unknown:
                raise ValueError(f"共享服务范围只支持 projects 项目白名单，不支持：{sorted(unknown)}")
            projects = scope.get("projects", [])
            if not isinstance(projects, list) or any(
                    not isinstance(item, str) or not item.strip() for item in projects):
                raise ValueError("scope.projects 必须是非空字符串数组")
            if len(set(projects)) != len(projects):
                raise ValueError("scope.projects 不能重复")
            scope = {"projects": list(projects)} if projects else {}
        if deployment["publish_state"] != "published":
            raise ValueError("只能绑定已发布的服务部署")
        actual_contract = self.deployment_contract(deployment)
        contract = contract or actual_contract
        if actual_contract and contract != actual_contract:
            raise ValueError(f"契约不匹配：部署为 {actual_contract}，绑定为 {contract}")
        known = deployment["config"].get("tools") or []
        if known:
            unknown = [tool for tool in tools if tool not in known]
            if unknown:
                raise ValueError(f"工具不在服务目录内：{unknown}；已知工具 {known}")
        if deployment["mode"] == "managed" and deployment.get("revision_id"):
            revision = self.services.revision(deployment["revision_id"])
            if revision and revision["manifest"].get("operations", {}).get("session_sharing") == "isolated":
                others = [b for b in self.services.bindings(deployment_id=deployment_id)
                          if b["enabled"] and b["agent_id"] != agent_id]
                if others:
                    raise ValueError("服务声明 session_sharing: isolated，不能共享部署给多个 Agent")
        binding = self.services.save_binding(agent_id=agent_id, deployment_id=deployment_id, alias=alias,
                                             service_id=deployment["service_id"], contract=contract,
                                             tools=tools, scope=scope, expected_revision=expected_revision)
        return binding

    def unbind(self, binding_id: str) -> dict:
        binding = self.services.binding(binding_id)
        if not binding:
            raise ValueError(f"绑定不存在：{binding_id}")
        disabled = self.services.save_binding(agent_id=binding["agent_id"], deployment_id=binding["deployment_id"],
                                              alias=binding["alias"], service_id=binding["service_id"],
                                              contract=binding["contract"], tools=binding["tools"],
                                              scope=binding["scope"], enabled=False,
                                              expected_revision=binding["revision"])
        # Immediate revocation: outstanding grants stop working at once.
        self.services.revoke_grants(deployment_id=binding["deployment_id"], agent_id=binding["agent_id"])
        deployment = self.services.deployment(binding["deployment_id"])
        if self.managed.is_builtin(deployment):
            from .knowledge_migration import revoke_builtin_grants
            revoke_builtin_grants(self.config, deployment, binding["agent_id"])
        return disabled

    # -- read models ----------------------------------------------------------
    def list_services(self) -> list[dict]:
        result = []
        for deployment in self.services.deployments():
            result.append(self.describe(deployment))
        return result

    def describe(self, deployment: dict) -> dict:
        bindings = self.services.bindings(deployment_id=deployment["id"])
        revision = self.services.revision(deployment["revision_id"]) if deployment.get("revision_id") else None
        return {"deployment_id": deployment["id"], "service_id": deployment["service_id"],
                "mode": deployment["mode"], "desired_state": deployment["desired_state"],
                "publish_state": deployment["publish_state"], "observed_state": deployment["observed_state"],
                "observed_at": deployment["observed_at"], "verification": deployment["verification"],
                "endpoint": None if deployment["mode"] == "external" else deployment["endpoint"],
                "external_url": deployment["config"].get("external_url") if deployment["mode"] == "external" else None,
                "contract": self.deployment_contract(deployment),
                "owner": deployment["config"].get("owner", ""),
                "tools": deployment["config"].get("tools", []),
                "generation": deployment["generation"],
                "maintenance": bool(deployment["maintenance"]),
                "commit": revision["commit_sha"][:12] if revision else None,
                "revision_id": deployment["revision_id"],
                "bindings": [{"binding_id": b["id"], "agent_id": b["agent_id"], "alias": b["alias"],
                              "tools": b["tools"], "enabled": b["enabled"], "revision": b["revision"]}
                             for b in bindings],
                "incidents": [i["id"] for i in self.services.incidents(deployment_id=deployment["id"],
                                                                       status="open")]}
