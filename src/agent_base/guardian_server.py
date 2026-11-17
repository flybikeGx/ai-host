"""Restricted operations MCP for the guardian agent.

The tool surface is a fixed whitelist; every write action re-validates target
deployment, policy and budget server-side. Passing different parameters can
never widen this into full Admin access: there is no shell, no credentials and
no installation tool here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .config import Config

STRING = {"type": "string", "minLength": 1}


def _schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = {
    "services_overview": ("列出全部共享部署的期望/发布/观测状态、验证等级、绑定数与未关闭故障单；附监督器心跳。",
                          _schema({})),
    "service_detail": ("查看一个部署的健康证据、最近健康事件、绑定与最近操作。", _schema({
        "deployment_id": STRING}, ("deployment_id",))),
    "read_service_log": ("读取部署日志末尾（脱敏、限量）；仅用于诊断。", _schema({
        "deployment_id": STRING, "name": STRING,
        "tail_bytes": {"type": "integer", "minimum": 100, "maximum": 32000}}, ("deployment_id", "name"))),
    "probe_service": ("立即执行三层探测并记录结果。", _schema({"deployment_id": STRING}, ("deployment_id",))),
    "restart_service": ("在策略允许时重启托管部署：需服务声明 restart_safe、部署授权看护重启、恢复预算足够且无在途调用。",
                        _schema({"deployment_id": STRING, "reason": STRING}, ("deployment_id", "reason"))),
    "list_incidents": ("列出故障单及其中文摘要。", _schema({"deployment_id": STRING}, ())),
    "record_incident_note": ("向未关闭故障单追加诊断记录（证据、结论或升级计划）。", _schema({
        "incident_id": STRING, "note": {"type": "string", "minLength": 1, "maxLength": 8000}}, ("incident_id", "note"))),
    "close_incident": ("关闭故障单；必须附上恢复验证证据。", _schema({
        "incident_id": STRING, "evidence": {"type": "string", "minLength": 1, "maxLength": 8000}},
        ("incident_id", "evidence"))),
    "escalate_incident": ("标记故障单需要管理员/责任人处理，并记录建议的处置计划。", _schema({
        "incident_id": STRING, "plan": {"type": "string", "minLength": 1, "maxLength": 8000}},
        ("incident_id", "plan"))),
}


def call(config: Config, name: str, args: dict):
    from .service_catalog import ServiceCatalog
    from .service_install import ManagedServices
    from .service_supervisor import ServiceSupervisor, probe_deployment
    from .service_store import ServiceStore
    catalog = ServiceCatalog(config)
    managed = catalog.managed
    stores = catalog.services
    if name == "services_overview":
        return {"supervisor": ServiceSupervisor.supervisor_state(config),
                "deployments": catalog.list_services()}
    if name == "service_detail":
        deployment = catalog._deployment(args["deployment_id"])
        return {**catalog.describe(deployment),
                "health": managed.gateway_health(deployment),
                "recent_health_events": stores.health_events(args["deployment_id"], limit=20),
                "incidents": stores.incidents(deployment_id=args["deployment_id"])}
    if name == "read_service_log":
        return managed.read_log(args["deployment_id"], args["name"], tail_bytes=args.get("tail_bytes", 12000))
    if name == "probe_service":
        return probe_deployment(config, args["deployment_id"])
    if name == "restart_service":
        # Server-side policy check: actor guardian cannot bypass anything.
        return managed.restart(args["deployment_id"], reason=f"guardian: {args['reason']}", actor="guardian")
    if name == "list_incidents":
        return stores.incidents(deployment_id=args.get("deployment_id"))
    if name == "record_incident_note":
        stores.record_incident_action(args["incident_id"], {"kind": "guardian_note", "note": args["note"]})
        return stores.incident(args["incident_id"])
    if name == "close_incident":
        stores.record_incident_action(args["incident_id"], {"kind": "guardian_close", "evidence": args["evidence"]})
        stores.update_incident(args["incident_id"], status="closed", close_evidence=args["evidence"])
        return stores.incident(args["incident_id"])
    if name == "escalate_incident":
        stores.record_incident_action(args["incident_id"], {"kind": "guardian_escalation", "plan": args["plan"]})
        return {"incident_id": args["incident_id"], "escalated": True}
    raise ValueError(f"未知看护工具：{name}")


async def serve(args):
    config = Config.load(args.config)
    server = Server("agent-base-guardian")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=name, description=description, inputSchema=spec)
                for name, (description, spec) in TOOLS.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        try:
            jsonschema.validate(arguments, TOOLS[name][1])
            result = await asyncio.to_thread(call, config, name, arguments)
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        except KeyError as error:
            raise ValueError(f"未知看护工具：{name}") from error
        except Exception as error:
            raise ValueError(str(error)[:400]) from error

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(prog="agent-base-guardian")
    parser.add_argument("--config", required=True)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
