"""schema_version: 3 manifests for managed MCP service packages.

`kind: mcp-service` packages describe how a shared service is installed,
started, probed and operated. They never require AGENTS.md; the administrator
reviews Installation.md plus the runbook declared in the manifest. `kind: agent`
manifests are validated by package.load_package and keep the agent contract.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import yaml

from .package import argv as argv_list, within

SERVICE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
CONTRACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
TOOL_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,127}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
SERVICE_FIELDS = {"id", "version", "contract", "title"}
HEALTH_FIELDS = {"interval_seconds", "timeout_seconds", "failure_threshold", "recovery_threshold",
                 "expected_tools", "probe_tool", "startup_timeout_seconds", "business_dependency"}
OPERATIONS_FIELDS = {"runbook", "restart_safe", "session_sharing", "drain_timeout_seconds"}
TRANSPORTS = {"stdio", "streamable_http"}
SESSION_SHARING = {"safe", "isolated"}
DEFAULT_HEALTH = {"interval_seconds": 30, "timeout_seconds": 5, "failure_threshold": 3,
                  "recovery_threshold": 2, "startup_timeout_seconds": 60}
PLACEHOLDERS = ("${runtime.python}", "${package.root}", "${service.port}", "${service.data}")


def config_block(root: Path) -> tuple[dict, str]:
    """The unique machine configuration: agent.yaml or one install block."""
    file = root / "agent.yaml"
    docs = [p for p in (root / "Installation.md", root / "installation.md") if p.is_file()]
    if len(docs) > 1 and not docs[0].samefile(docs[1]):
        raise ValueError("只允许一个 Installation.md/installation.md")
    import re as _re
    blocks = _re.findall(r"^```agent-base-install\s*\n(.*?)^```\s*$", docs[0].read_text(encoding="utf-8"), _re.M | _re.S) if docs else []
    if len(blocks) > 1 or (file.exists() and blocks):
        raise ValueError("只能有一个机器配置来源：agent.yaml 或 Installation.md 中唯一的 agent-base-install 配置块")
    if file.exists():
        return yaml.safe_load(file.read_text(encoding="utf-8")), "agent.yaml"
    if blocks:
        return yaml.safe_load(blocks[0]), "Installation.md"
    raise ValueError(f"缺少机器配置（agent.yaml 或 Installation.md 配置块）：{root}")


def _placeholders(argv: list[str], allowed: set[str]):
    for value in argv:
        for match in re.findall(r"\$\{[^}]+\}", value):
            if match not in allowed:
                raise ValueError(f"不支持的占位符：{match}")


def _env_values(values, label: str) -> dict:
    if not isinstance(values, dict):
        raise ValueError(f"{label} 必须是对象")
    result = {}
    for name, value in values.items():
        if not ENV_NAME.fullmatch(str(name)):
            raise ValueError(f"{label} 环境变量名无效：{name}")
        if isinstance(value, dict) and set(value) == {"from_env"}:
            if not isinstance(value["from_env"], str) or not ENV_NAME.fullmatch(value["from_env"]):
                raise ValueError(f"{label}.{name}.from_env 必须是环境变量名")
            result[name] = {"from_env": value["from_env"]}
        elif isinstance(value, str):
            result[name] = value
        else:
            raise ValueError(f"{label}.{name} 须为字符串或 from_env 引用")
    return result


def _positive(value, label: str, *, minimum=0.5, maximum=86400) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须在 {minimum}–{maximum} 之间")
    return value


def load_service_manifest(root: Path) -> dict:
    """Parse and validate one mcp-service component manifest (read-only)."""
    root = Path(root)
    manifest, _source = config_block(root)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 3:
        raise ValueError("服务清单必须是 schema_version: 3 的对象")
    kind = manifest.get("kind")
    if kind == "agent":
        raise ValueError("kind: agent 组件请使用 agent.yaml/Installation.md 的 agent 安装流程（load_package）")
    if kind != "mcp-service":
        raise ValueError("服务清单 kind 必须是 mcp-service")
    unknown = set(manifest) - {"schema_version", "kind", "service", "environment", "installation", "runtime",
                               "health", "operations"}
    if unknown:
        raise ValueError(f"未知服务清单字段：{sorted(unknown)}")
    service = manifest.get("service")
    if not isinstance(service, dict) or set(service) - SERVICE_FIELDS:
        raise ValueError("service 段字段不正确")
    if not SERVICE_ID.fullmatch(service.get("id", "")):
        raise ValueError("service.id 必须是小写字母、数字和连字符")
    if not CONTRACT.fullmatch(service.get("contract", "")):
        raise ValueError("service.contract 必须形如 ticket-center/v1")
    if "version" in service and (not isinstance(service["version"], str) or not service["version"].strip()):
        raise ValueError("service.version 必须是非空文本")

    environment = manifest.get("environment", {})
    if not isinstance(environment, dict) or set(environment) - {"lockfile"}:
        raise ValueError("environment 段只支持 lockfile")
    lockfile = environment.get("lockfile")
    if lockfile:
        if lockfile != "uv.lock":
            raise ValueError("首版服务环境安装支持 uv.lock + pyproject.toml")
        within(root, lockfile)
        within(root, "pyproject.toml")

    installation = manifest.get("installation", {})
    if not isinstance(installation, dict) or set(installation) - {"steps"}:
        raise ValueError("installation 段字段不正确")
    steps = installation.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("installation.steps 必须是数组")
    for step in steps:
        argv = argv_list(step.get("argv"))
        _placeholders(argv, {"${runtime.python}", "${package.root}", "${service.data}"})
        if step.get("timeout_seconds") is not None:
            _positive(step["timeout_seconds"], "installation.steps.timeout_seconds")

    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) - {"transport", "argv", "environment", "endpoint_path"}:
        raise ValueError("runtime 段字段不正确")
    transport = runtime.get("transport")
    if transport not in TRANSPORTS:
        raise ValueError("runtime.transport 必须是 stdio 或 streamable_http")
    argv = argv_list(runtime.get("argv"))
    allowed = {"${runtime.python}", "${package.root}", "${service.data}"} | ({"${service.port}"} if transport == "streamable_http" else set())
    _placeholders(argv, allowed)
    if transport == "streamable_http" and "${service.port}" not in " ".join(argv):
        raise ValueError("原生 HTTP 服务的 argv 需要使用 ${service.port} 接收平台分配的端口")
    if transport == "stdio" and "${service.port}" in " ".join(argv):
        raise ValueError("stdio 服务不能使用 ${service.port}")
    _env_values(runtime.get("environment", {}), "runtime.environment")
    if runtime.get("endpoint_path") is not None:
        path = runtime["endpoint_path"]
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9._/-]{0,63}", path):
            raise ValueError("runtime.endpoint_path 必须是 / 开头的简单路径")

    health = manifest.get("health", {})
    if not isinstance(health, dict) or set(health) - HEALTH_FIELDS:
        raise ValueError("health 段字段不正确")
    for key in ("interval_seconds", "timeout_seconds", "startup_timeout_seconds"):
        if key in health:
            _positive(health[key], f"health.{key}")
    for key in ("failure_threshold", "recovery_threshold"):
        if key in health and (not isinstance(health[key], int) or isinstance(health[key], bool) or not 1 <= health[key] <= 100):
            raise ValueError(f"health.{key} 必须是 1–100 的整数")
    expected = health.get("expected_tools", [])
    if not isinstance(expected, list) or not all(isinstance(t, str) and TOOL_NAME.fullmatch(t) for t in expected):
        raise ValueError("health.expected_tools 必须是工具名数组")
    if len(set(expected)) != len(expected):
        raise ValueError("health.expected_tools 不能重复")
    probe = health.get("probe_tool")
    if probe is not None:
        if not isinstance(probe, dict) or set(probe) - {"tool", "arguments"}:
            raise ValueError("health.probe_tool 字段不正确")
        if not isinstance(probe.get("tool"), str) or not TOOL_NAME.fullmatch(probe["tool"]):
            raise ValueError("health.probe_tool.tool 必须是工具名")
        if not isinstance(probe.get("arguments", {}), dict):
            raise ValueError("health.probe_tool.arguments 必须是对象")
        if expected and probe["tool"] not in expected:
            raise ValueError("health.probe_tool.tool 必须在 expected_tools 内")

    operations = manifest.get("operations", {})
    if not isinstance(operations, dict) or set(operations) - OPERATIONS_FIELDS:
        raise ValueError("operations 段字段不正确")
    if operations.get("runbook"):
        within(root, operations["runbook"])
    if operations.get("restart_safe") is not None and not isinstance(operations["restart_safe"], bool):
        raise ValueError("operations.restart_safe 必须是布尔值")
    if operations.get("session_sharing") is not None and operations["session_sharing"] not in SESSION_SHARING:
        raise ValueError("operations.session_sharing 必须是 safe 或 isolated")
    if operations.get("drain_timeout_seconds") is not None:
        _positive(operations["drain_timeout_seconds"], "operations.drain_timeout_seconds")
    return manifest


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode()).hexdigest()[:32]


def service_expand(values: list[str], *, package: Path, python: str, data_dir: Path, port: int | None = None) -> list[str]:
    """Expand service placeholders with fixed, validated inputs."""
    replacements = {"${runtime.python}": python, "${package.root}": str(package), "${service.data}": str(data_dir)}
    if port is not None:
        replacements["${service.port}"] = str(port)
    result = []
    for value in argv_list(values):
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        if "${" in value:
            raise ValueError(f"存在未解析的占位符：{value}")
        result.append(value)
    return result


def freeze_service_source(workspace: Path, target: Path) -> dict:
    """Copy the component source, then re-validate against the frozen copy."""
    if target.exists():
        raise ValueError(f"目标已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(workspace, target, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", ".env", "__pycache__"), symlinks=True)
    manifest = load_service_manifest(target)
    (target / "resolved.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def runbook_text(root: Path, manifest: dict) -> str | None:
    path = manifest.get("operations", {}).get("runbook")
    if not path:
        return None
    file = within(Path(root), path)
    if file.stat().st_size > 128 * 1024:
        raise ValueError(f"runbook 超过 128 KiB：{path}")
    return file.read_text(encoding="utf-8")
