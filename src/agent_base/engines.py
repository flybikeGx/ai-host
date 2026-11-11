from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .package import expand


@dataclass
class JsEnvironment:
    name: str


class PatchDumper(yaml.SafeDumper):
    pass


class PatchLoader(yaml.SafeLoader):
    pass


def load_environment(loader, node):
    value = loader.construct_scalar(node)
    if not value.startswith('process.env[') or not value.endswith(']'):
        raise ValueError('仅允许环境变量引用的 patch 表达式')
    name = json.loads(value[len('process.env['):-1])
    if not isinstance(name, str):
        raise ValueError('环境变量引用格式错误')
    return JsEnvironment(name)


PatchLoader.add_constructor('tag:yaml.org,2002:js', load_environment)


PatchDumper.add_representer(JsEnvironment, lambda dumper, item: dumper.represent_scalar(
    "tag:yaml.org,2002:js", "process.env[" + json.dumps(item.name) + "]"))


def environment_values(values: dict) -> dict:
    result = {}
    for name, value in values.items():
        if isinstance(value, dict) and set(value) == {"from_env"}:
            key = value["from_env"]
            if not isinstance(key, str) or not os.environ.get(key):
                raise ValueError(f"缺少环境凭据：{key}（只设置环境变量，不写入清单）")
            result[name] = JsEnvironment(key)
        elif isinstance(value, str):
            result[name] = value
        else:
            raise ValueError("环境值须为字符串或 {from_env: 环境变量名}")
    return result


def dsh_patch(runtime: dict, root: Path, *, package: Path | None = None,
              workspace: Path | None = None, manifest: dict | None = None,
              python: str = sys.executable, admin_config: Path | None = None) -> Path:
    rows = [{"id": "agent-default-model", "config": {
        "provider": runtime.get("provider", "deepseek-official"), "model": runtime["model"],
        **({"reasoningEffort": runtime["effort"]} if runtime.get("effort") else {})}},
        {"id": "approval", "config": {"policy": runtime.get("approval_policy", "never")}}]
    inserts = []
    if admin_config:
        admin_env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            if os.environ.get(name):
                admin_env[name] = JsEnvironment(name)
        rows.append({"id": "agent-instructions", "config": {"maxBytes": 65536, "projectRootMarkers": [".agent-base-root"]}})
        inserts.append({"id": "agent-base-admin", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "admin", "transport": "stdio", "command": sys.executable,
            "args": ["-m", "agent_base.mcp_server", "--config", str(admin_config)],
            "env": admin_env,
            "startupTimeoutMs": 30000, "failOnStartupError": True}})
    if manifest and package and workspace:
        if manifest.get("tools", {}).get("local"):
            inserts.append({"id": "agent-base-local-tools", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                "serverName": "local", "transport": "stdio", "command": sys.executable,
                "args": ["-m", "agent_base.mcp_server", "--package", str(package), "--workspace", str(workspace), "--python", python],
                "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                "startupTimeoutMs": 30000, "failOnStartupError": True}})
        for server in manifest.get("mcp", {}).get("servers", []):
            config = {"serverName": server["id"], "transport": "streamable-http" if server["transport"] == "streamable_http" else "stdio",
                      "startupTimeoutMs": 30000, "toolCallTimeoutMs": server.get("timeout_seconds", 60) * 1000,
                      "failOnStartupError": True}
            if server["transport"] == "stdio":
                args = expand(server["argv"], package, workspace, python)
                config.update(command=args[0], args=args[1:], cwd=str(workspace), env=environment_values(server.get("environment", {})))
            else:
                config.update(url=server["url"], headers=environment_values(server.get("headers", {})))
            inserts.append({"id": "agent-base-mcp-" + server["id"], "name": "@deepseek-ai/dsh-mcp-client", "config": config})
    if inserts:
        rows.append({"insert": inserts})
    patch = root / "runtime.patch.yml"
    patch.write_text(yaml.dump(rows, Dumper=PatchDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return patch


def dsh_command(runtime: dict, patch: Path, *, session_id: str | None = None, extra_root: Path | None = None) -> list[str]:
    # npm 0.2.1-rc.5 does not accept the newer --permission-mode/--add-dir
    # switches. Permission uses its documented environment + approval patch.
    args = [*runtime["command"], "--profile", "headless", "--patch", str(patch), "--json"]
    if session_id:
        args += ["--session-id", session_id]
    return [*args, "-"]


def runtime_environment(runtime: dict, home: Path) -> dict:
    env = dict(os.environ)
    # Make the package importable in child MCP/runner processes, including before
    # a user has installed the project in editable mode.
    source = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    for key in list(env):
        if key.startswith(("CLAUDE_HUB_", "CODEAGENT_HUB_", "AGENT_BASE_KNOWLEDGE_")) or key in {"CODEAGENT3_LAUNCHER_PID", "AGENT_BASE_WEB_TOKEN"}:
            del env[key]
    if runtime.get("engine", "dsh") == "dsh":
        env.update(DSH_HOME=str(home), DSH_AGENTS_HOME=str(home / "agents"), DSH_TELEMETRY_DISABLED="1",
                   DSH_PERMISSION_MODE=runtime.get("permission_mode", "workspace-write"))
    elif runtime.get("config_dir"):
        env["CODEAGENT3_CONFIG_DIR"] = str(Path(runtime["config_dir"]).expanduser().resolve())
    return env


def codeagent_command(runtime: dict) -> list[str]:
    # Explicit adapter seam. These flags are the known print-mode path from the
    # Huawei Hub; the current internal CLI still requires on-site acceptance.
    return [*runtime["command"], "--print", "--output-format", "text", "--disable-update", "--skip-safe-check",
            "--no-session-persistence", "--model", runtime["model"], "--effort", runtime["effort"]]


def parse_dsh_output(file: Path) -> dict:
    session = None
    final = None
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as error:
            raise RuntimeError("dsh stdout 不是有效 NDJSON，保留原始日志供核对") from error
        if not isinstance(item, dict):
            raise RuntimeError("dsh 事件必须是 JSON 对象")
        if item.get("type") == "error":
            raise RuntimeError("dsh 返回 error 事件；请查看原始日志")
        if item.get("type") == "session":
            if session is not None:
                raise RuntimeError("dsh 返回多个 session，不能确认任务身份")
            session = item.get("sessionId")
        elif item.get("type") == "final":
            if final is not None:
                raise RuntimeError("dsh 返回多个 final，不能确认任务结果")
            final = item
    if final is None or not session:
        raise RuntimeError("dsh 未返回 session/final；请查看 runtime.jsonl / runtime.stderr.log")
    if not isinstance(final.get("text"), str):
        raise RuntimeError("dsh final.text 必须是字符串")
    if "reason" in final and (not isinstance(final["reason"], dict) or final["reason"].get("kind") != "completed"):
        raise RuntimeError("dsh final 不是 completed；请查看 runtime.jsonl")
    if "sessionId" in final and final["sessionId"] != session:
        raise RuntimeError("dsh final 的会话身份不匹配")
    return {"text": final.get("text", ""), "session_id": session,
            "finish_reason": final.get("reason"), "event_contract": "typed_final" if "reason" in final else "legacy_final_exit_code"}
