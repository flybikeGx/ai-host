"""First-party MCP package manifests: discovery, validation and loaders.

AIHO-33 D1 prototype. This module is deliberately read-only with respect to
business code: it parses ``mcp.yaml`` package manifests, validates the package
contract and merges package declarations with the persisted installation state.
It does not start services, move data or wire navigation; D2/D3 add those
consumers. See ``docs/MCP_PACKAGE.md`` for the frozen contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from .package import within

PACKAGE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
PACKAGE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
CONTRACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
SCOPE = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]*)+")
TOOL = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,127}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ROUTE = re.compile(r"/[a-z][a-z0-9-]{0,31}(?:/[a-z][a-z0-9-]{0,31}){0,2}")
ICON = re.compile(r"[a-z][a-z0-9-]{0,31}")
DATA_DIR = re.compile(r"[a-z][a-z0-9._-]{0,63}")
ENDPOINT_PATH = re.compile(r"/[A-Za-z0-9._/-]{0,63}")
REQUIRES_AGENT_BASE = re.compile(r"(?:>=|<=|==|>|<|\^|~)?[0-9]+(?:\.[0-9]+){0,2}(?:[ ,]+(?:>=|<=|==|>|<|\^|~)?[0-9]+(?:\.[0-9]+){0,2})*")

MANAGED_ROOT = "first_party"
MANIFEST_NAME = "mcp.yaml"
STATE_FILE = "installations.json"

TOP_FIELDS = {"schema_version", "kind", "id", "title", "version", "contract", "description",
              "service", "frontend", "installation", "permissions"}
SERVICE_FIELDS = {"transport", "runtime", "health", "data", "operations"}
RUNTIME_FIELDS = {"argv", "endpoint_path", "environment"}
HEALTH_FIELDS = {"path", "expected_tools", "probe_tool", "startup_timeout_seconds",
                 "interval_seconds", "timeout_seconds"}
PROBE_FIELDS = {"tool", "arguments"}
DATA_FIELDS = {"dir", "retention"}
OPERATIONS_FIELDS = {"restart_safe", "session_sharing", "drain_timeout_seconds"}
FRONTEND_FIELDS = {"mount", "route", "navigation", "entry", "assets", "requires", "standalone"}
NAVIGATION_FIELDS = {"label", "icon", "order"}
INSTALLATION_FIELDS = {"default", "steps", "upgrade", "uninstall", "requires_agent_base"}
STEP_FIELDS = {"argv", "timeout_seconds"}
PERMISSION_FIELDS = {"anonymous", "session", "api", "service_tools"}

TRANSPORTS = {"streamable_http", "stdio"}
MOUNTS = {"spa-module", "static-iframe"}
DEFAULT_STATES = {"installed", "available"}
UPGRADES = {"restart", "drain-restart", "manual"}
UNINSTALLS = {"keep-data", "purge-allowed"}
SESSIONS = {"same-origin-cookie", "capability-token"}
RETENTIONS = {"keep", "purge-allowed"}
SESSION_SHARING = {"safe", "isolated"}

# Platform-owned page routes. Package routes may never shadow them.
RESERVED_ROUTES = {"/chat", "/cron", "/dashboard", "/services", "/login", "/workbench",
                   "/admin", "/api", "/mcp-ui"}
RESERVED_PREFIXES = ("/api/", "/mcp-ui/", "/admin/")

RUNTIME_PLACEHOLDERS = {"${runtime.python}", "${runtime.config}", "${package.root}",
                        "${service.data}", "${service.port}"}
INSTALL_PLACEHOLDERS = {"${runtime.python}", "${runtime.config}", "${package.root}", "${service.data}"}

DEFAULT_HEALTH = {"path": "/health", "startup_timeout_seconds": 60,
                  "interval_seconds": 30, "timeout_seconds": 5}


class PackageError(ValueError):
    """A package manifest or installation state is invalid."""


@dataclass(frozen=True)
class McpPackage:
    root: Path
    manifest_path: Path
    manifest: dict

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def title(self) -> str:
        return self.manifest["title"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def contract(self) -> str:
        return self.manifest["contract"]

    @property
    def frontend(self) -> dict | None:
        return self.manifest.get("frontend")

    @property
    def route(self) -> str | None:
        frontend = self.frontend
        return frontend.get("route") if frontend else None

    @property
    def mount(self) -> str | None:
        frontend = self.frontend
        return frontend.get("mount") if frontend else None

    @property
    def data_dir(self) -> str:
        return self.manifest["service"]["data"]["dir"]

    @property
    def default_installation(self) -> str:
        return self.manifest["installation"]["default"]

    @property
    def digest(self) -> str:
        return manifest_digest(self.manifest)

    def navigation(self) -> dict | None:
        frontend = self.frontend
        if not frontend or frontend["mount"] != "spa-module":
            return None
        navigation = frontend["navigation"]
        return {"id": self.id, "route": frontend["route"], "label": navigation["label"],
                "icon": navigation["icon"], "order": navigation["order"]}


@dataclass(frozen=True)
class Installation:
    installed: bool
    enabled: bool
    source: str
    version: str | None = None
    manifest_digest: str | None = None
    updated_at: float | None = None
    stale: bool = False


def _fields(value, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise PackageError(f"{label} 必须是对象")
    unknown = set(value) - allowed
    if unknown:
        raise PackageError(f"{label} 包含未知字段：{sorted(unknown)}")
    return value


def _text(value, label: str, pattern: re.Pattern | None = None, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PackageError(f"{label} 必须是 1–{maximum} 字符的文本")
    if pattern is not None and not pattern.fullmatch(value):
        raise PackageError(f"{label} 格式不正确：{value!r}")
    return value


def _boolean(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise PackageError(f"{label} 必须是布尔值")
    return value


def _integer(value, label: str, *, minimum: int = 0, maximum: int = 1000) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PackageError(f"{label} 必须是 {minimum}–{maximum} 的整数")
    return value


def _positive(value, label: str, *, minimum: float = 0.5, maximum: float = 86400) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise PackageError(f"{label} 必须在 {minimum}–{maximum} 之间")
    return value


def _argv(value, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item and "\0" not in item for item in value):
        raise PackageError(f"{label} 必须是非空字符串数组，不能是 shell 命令字符串")
    return value


def _placeholders(argv: list[str], allowed: set[str], label: str) -> None:
    for value in argv:
        for match in re.findall(r"\$\{[^}]+\}", value):
            if match not in allowed:
                raise PackageError(f"{label} 包含不支持的占位符：{match}")


def _env(values, label: str) -> dict:
    if not isinstance(values, dict):
        raise PackageError(f"{label} 必须是对象")
    result = {}
    for name, value in values.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise PackageError(f"{label} 环境变量名无效：{name}")
        if isinstance(value, dict) and set(value) == {"from_env"}:
            if not isinstance(value["from_env"], str) or not ENV_NAME.fullmatch(value["from_env"]):
                raise PackageError(f"{label}.{name}.from_env 必须是环境变量名")
            result[name] = {"from_env": value["from_env"]}
        elif isinstance(value, str):
            result[name] = value
        else:
            raise PackageError(f"{label}.{name} 须为字符串或 from_env 引用")
    return result


def _validate_service(service, root: Path, *, has_frontend: bool) -> None:
    _fields(service, SERVICE_FIELDS, "service")
    transport = service.get("transport")
    if transport not in TRANSPORTS:
        raise PackageError("service.transport 必须是 streamable_http 或 stdio")

    runtime = _fields(service.get("runtime"), RUNTIME_FIELDS, "service.runtime")
    argv = _argv(runtime.get("argv"), "service.runtime.argv")
    _placeholders(argv, RUNTIME_PLACEHOLDERS, "service.runtime.argv")
    if runtime.get("endpoint_path") is not None:
        _text(runtime["endpoint_path"], "service.runtime.endpoint_path", ENDPOINT_PATH, maximum=64)
    _env(runtime.get("environment", {}), "service.runtime.environment")

    health = _fields(service.get("health", {}), HEALTH_FIELDS, "service.health")
    if "path" in health:
        _text(health["path"], "service.health.path", ENDPOINT_PATH, maximum=64)
    for key in ("startup_timeout_seconds", "interval_seconds", "timeout_seconds"):
        if key in health:
            _positive(health[key], f"service.health.{key}")
    expected = health.get("expected_tools")
    if not isinstance(expected, list) or not expected or not all(
            isinstance(item, str) and TOOL.fullmatch(item) for item in expected):
        raise PackageError("service.health.expected_tools 必须是非空工具名数组")
    if len(set(expected)) != len(expected):
        raise PackageError("service.health.expected_tools 不能重复")
    probe = health.get("probe_tool")
    if probe is not None:
        _fields(probe, PROBE_FIELDS, "service.health.probe_tool")
        if not isinstance(probe.get("tool"), str) or not TOOL.fullmatch(probe["tool"]):
            raise PackageError("service.health.probe_tool.tool 必须是工具名")
        if probe["tool"] not in expected:
            raise PackageError("service.health.probe_tool.tool 必须在 expected_tools 内")
        if not isinstance(probe.get("arguments", {}), dict):
            raise PackageError("service.health.probe_tool.arguments 必须是对象")

    data = _fields(service.get("data", {}), DATA_FIELDS, "service.data")
    if "dir" in data:
        _text(data["dir"], "service.data.dir", DATA_DIR, maximum=64)
    if data.get("retention", "keep") not in RETENTIONS:
        raise PackageError("service.data.retention 必须是 keep 或 purge-allowed")

    operations = _fields(service.get("operations", {}), OPERATIONS_FIELDS, "service.operations")
    if operations.get("restart_safe") is not None:
        _boolean(operations["restart_safe"], "service.operations.restart_safe")
    if operations.get("session_sharing") is not None and operations["session_sharing"] not in SESSION_SHARING:
        raise PackageError("service.operations.session_sharing 必须是 safe 或 isolated")
    if operations.get("drain_timeout_seconds") is not None:
        _positive(operations["drain_timeout_seconds"], "service.operations.drain_timeout_seconds")

    # A package that ships a human frontend is a Streamable HTTP service.
    if has_frontend and transport != "streamable_http":
        raise PackageError("带前端的包必须使用 streamable_http")


def _validate_frontend(frontend, root: Path) -> None:
    _fields(frontend, FRONTEND_FIELDS, "frontend")
    mount = frontend.get("mount")
    if mount not in MOUNTS:
        raise PackageError("frontend.mount 必须是 spa-module 或 static-iframe")
    route = _text(frontend.get("route"), "frontend.route", ROUTE, maximum=96)
    first = "/" + route.strip("/").split("/", 1)[0]
    if first in RESERVED_ROUTES or route.startswith(RESERVED_PREFIXES):
        raise PackageError(f"frontend.route 占用了平台保留路由：{route}")

    navigation = _fields(frontend.get("navigation"), NAVIGATION_FIELDS, "frontend.navigation")
    _text(navigation.get("label"), "frontend.navigation.label", maximum=32)
    _text(navigation.get("icon"), "frontend.navigation.icon", ICON, maximum=32)
    _integer(navigation.get("order", 100), "frontend.navigation.order")

    requires = frontend.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(item, str) and SCOPE.fullmatch(item) for item in requires):
        raise PackageError("frontend.requires 必须是权限名数组")
    if len(set(requires)) != len(requires):
        raise PackageError("frontend.requires 不能重复")

    standalone = frontend.get("standalone", mount == "static-iframe")
    _boolean(standalone, "frontend.standalone")
    if mount == "spa-module":
        if standalone:
            raise PackageError("spa-module 的 frontend.standalone 必须为 false")
        _text(frontend.get("entry"), "frontend.entry", maximum=256)
        try:
            within(root, frontend["entry"], exists=True)
        except ValueError as error:
            raise PackageError(f"frontend.entry 无效：{error}") from error
        if frontend.get("assets") is not None:
            raise PackageError("spa-module 不接受 frontend.assets")
    else:
        if not standalone:
            raise PackageError("static-iframe 的 frontend.standalone 必须为 true")
        _text(frontend.get("assets"), "frontend.assets", maximum=256)
        try:
            within(root, frontend["assets"], exists=False)
        except ValueError as error:
            raise PackageError(f"frontend.assets 无效：{error}") from error
        if frontend.get("entry") is not None:
            raise PackageError("static-iframe 不接受 frontend.entry；入口是 assets/index.html")


def _validate_installation(installation) -> None:
    _fields(installation, INSTALLATION_FIELDS, "installation")
    if installation.get("default", "available") not in DEFAULT_STATES:
        raise PackageError("installation.default 必须是 installed 或 available")
    steps = installation.get("steps", [])
    if not isinstance(steps, list):
        raise PackageError("installation.steps 必须是数组")
    for step in steps:
        _fields(step, STEP_FIELDS, "installation.steps[]")
        argv = _argv(step.get("argv"), "installation.steps[].argv")
        _placeholders(argv, INSTALL_PLACEHOLDERS, "installation.steps[].argv")
        if step.get("timeout_seconds") is not None:
            _positive(step["timeout_seconds"], "installation.steps[].timeout_seconds")
    if installation.get("upgrade", "restart") not in UPGRADES:
        raise PackageError("installation.upgrade 必须是 restart、drain-restart 或 manual")
    if installation.get("uninstall", "keep-data") not in UNINSTALLS:
        raise PackageError("installation.uninstall 必须是 keep-data 或 purge-allowed")
    if installation.get("requires_agent_base") is not None:
        _text(installation["requires_agent_base"], "installation.requires_agent_base",
              REQUIRES_AGENT_BASE, maximum=64)


def _validate_permissions(permissions, mount: str | None) -> None:
    _fields(permissions, PERMISSION_FIELDS, "permissions")
    if _boolean(permissions.get("anonymous", False), "permissions.anonymous"):
        raise PackageError("permissions.anonymous 在 v1 只允许 false")
    session = permissions.get("session",
                              "capability-token" if mount == "static-iframe" else "same-origin-cookie")
    if session not in SESSIONS:
        raise PackageError("permissions.session 必须是 same-origin-cookie 或 capability-token")
    if mount == "static-iframe" and session != "capability-token":
        raise PackageError("static-iframe 必须使用 capability-token")
    if mount == "spa-module" and session != "same-origin-cookie":
        raise PackageError("spa-module 必须使用 same-origin-cookie")
    api = permissions.get("api", [])
    if not isinstance(api, list) or not all(isinstance(item, str) and SCOPE.fullmatch(item) for item in api):
        raise PackageError("permissions.api 必须是权限名数组")
    if len(set(api)) != len(api):
        raise PackageError("permissions.api 不能重复")
    tools = permissions.get("service_tools", [])
    if not isinstance(tools, list) or not all(isinstance(item, str) and TOOL.fullmatch(item) for item in tools):
        raise PackageError("permissions.service_tools 必须是工具名数组")
    if len(set(tools)) != len(tools):
        raise PackageError("permissions.service_tools 不能重复")


def load_package(path: Path) -> McpPackage:
    """Parse and validate one package directory or one ``mcp.yaml`` file."""
    path = Path(path)
    manifest_path = path / MANIFEST_NAME if path.is_dir() else path
    if not manifest_path.is_file():
        raise PackageError(f"缺少 {MANIFEST_NAME}：{path}")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackageError(f"无法读取清单 {manifest_path}：{error}") from error
    if not isinstance(manifest, dict):
        raise PackageError(f"清单必须是对象：{manifest_path}")
    _fields(manifest, TOP_FIELDS, "mcp.yaml")
    if manifest.get("schema_version") != 1:
        raise PackageError("mcp.yaml 的 schema_version 必须是 1")
    if manifest.get("kind") != "mcp-package":
        raise PackageError("mcp.yaml 的 kind 必须是 mcp-package")
    _text(manifest.get("id"), "id", PACKAGE_ID, maximum=64)
    _text(manifest.get("title"), "title", maximum=64)
    _text(manifest.get("version"), "version", PACKAGE_VERSION, maximum=64)
    _text(manifest.get("contract"), "contract", CONTRACT, maximum=128)
    if manifest.get("description") is not None:
        _text(manifest["description"], "description", maximum=200)

    root = manifest_path.parent
    frontend = manifest.get("frontend")
    _validate_service(manifest.get("service"), root, has_frontend=frontend is not None)
    if frontend is not None:
        _validate_frontend(frontend, root)
    _validate_installation(manifest.get("installation", {}))
    permissions = manifest.get("permissions")
    if permissions is None:
        raise PackageError("缺少 permissions 段")
    mount = frontend.get("mount") if frontend else None
    _validate_permissions(permissions, mount)

    service = manifest["service"]
    data = service.get("data", {})
    data["dir"] = data.get("dir", manifest["id"])
    data["retention"] = data.get("retention", "keep")
    service["data"] = data
    service["health"] = {**DEFAULT_HEALTH, **service.get("health", {})}
    manifest["installation"] = {"default": "available", "steps": [], "upgrade": "restart",
                                "uninstall": "keep-data", **manifest.get("installation", {})}

    expected = set(service["health"]["expected_tools"])
    declared = set(permissions.get("service_tools", []))
    if expected - declared:
        raise PackageError(f"expected_tools 必须包含在 permissions.service_tools 内：{sorted(expected - declared)}")
    if frontend:
        missing = set(frontend.get("requires", [])) - set(permissions.get("api", []))
        if missing:
            raise PackageError(f"frontend.requires 必须是 permissions.api 的子集：{sorted(missing)}")
    return McpPackage(root=root, manifest_path=manifest_path, manifest=manifest)


def discover(root: Path) -> dict[str, McpPackage]:
    """Load every non-underscore package directory under ``root``.

    Directories without ``mcp.yaml`` are ignored (they may be migration
    placeholders); callers that want a strict tree report use ``skipped``.
    """
    root = Path(root)
    if not root.is_dir():
        raise PackageError(f"包根目录不存在：{root}")
    packages: dict[str, McpPackage] = {}
    routes: dict[str, str] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if not (child / MANIFEST_NAME).is_file():
            continue
        package = load_package(child)
        expected = package.id.replace("-", "_")
        if child.name != expected:
            raise PackageError(f"包目录名必须是 {expected}，实际为 {child.name}")
        if package.id in packages:
            raise PackageError(f"包 id 重复：{package.id}")
        if package.route:
            if package.route in routes:
                raise PackageError(f"frontend.route 重复：{package.route}（{routes[package.route]} / {package.id}）")
            routes[package.route] = package.id
        packages[package.id] = package
    return packages


def skipped(root: Path) -> list[str]:
    root = Path(root)
    return sorted(child.name for child in root.iterdir()
                  if child.is_dir() and not child.name.startswith(("_", "."))
                  and not (child / MANIFEST_NAME).is_file())


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=True, sort_keys=True).encode()).hexdigest()[:32]


def expand_runtime(package: McpPackage, *, python: str, config_path: Path,
                   data_dir: Path, port: int | None = None) -> list[str]:
    """Expand ``service.runtime.argv`` with validated, non-shell placeholders."""
    replacements = {"${runtime.python}": str(python), "${runtime.config}": str(config_path),
                    "${package.root}": str(package.root), "${service.data}": str(data_dir)}
    if port is not None:
        replacements["${service.port}"] = str(port)
    result = []
    for value in package.manifest["service"]["runtime"]["argv"]:
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        if "${" in value:
            raise PackageError(f"存在未解析的占位符：{value}")
        result.append(value)
    return result


def expand_install_step(package: McpPackage, step: dict, *, python: str, config_path: Path,
                        data_dir: Path) -> list[str]:
    replacements = {"${runtime.python}": str(python), "${runtime.config}": str(config_path),
                    "${package.root}": str(package.root), "${service.data}": str(data_dir)}
    result = []
    for value in step["argv"]:
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        if "${" in value:
            raise PackageError(f"存在未解析的占位符：{value}")
        result.append(value)
    return result


class Installations:
    """Persisted package state at ``<state_root>/mcp/installations.json``.

    Missing records fall back to the manifest default so the three existing
    first-party MCPs stay installed and enabled without a migration command.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.file = self.root / STATE_FILE
        self.lock = self.root / "installations.lock"

    def _empty(self) -> dict:
        return {"schema_version": 1, "revision": 0, "packages": {}}

    def read(self) -> dict:
        try:
            value = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError):
            # Never overwrite a corrupt file silently; callers can inspect/recover.
            return self._empty()
        if not isinstance(value, dict) or value.get("schema_version") != 1 \
                or not isinstance(value.get("packages"), dict):
            return self._empty()
        return value

    def effective(self, package: McpPackage) -> Installation:
        record = self.read()["packages"].get(package.id)
        if record is not None:
            installed = bool(record.get("installed"))
            enabled = bool(record.get("enabled")) and installed
            stale = record.get("version") != package.version or record.get("manifest_digest") != package.digest
            return Installation(installed=installed, enabled=enabled,
                                source=str(record.get("source", "state")),
                                version=record.get("version"), manifest_digest=record.get("manifest_digest"),
                                updated_at=record.get("updated_at"), stale=stale)
        installed = package.default_installation == "installed"
        return Installation(installed=installed, enabled=installed, source="default")

    def _write(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.file)

    def set(self, package: McpPackage, *, installed: bool | None = None, enabled: bool | None = None,
            source: str = "manual") -> Installation:
        from .repositories import installation_lock
        with installation_lock(self.lock, 30):
            state = self.read()
            current = self.effective(package)
            target_installed = current.installed if installed is None else installed
            target_enabled = current.enabled if enabled is None else enabled
            if target_enabled and not target_installed:
                raise PackageError("未安装的包不能启用")
            state["packages"][package.id] = {
                "installed": target_installed,
                "enabled": target_enabled,
                "version": package.version,
                "manifest_digest": package.digest,
                "source": source,
                "updated_at": int(time.time()),
            }
            state["revision"] = int(state.get("revision", 0)) + 1
            self._write(state)
        return self.effective(package)

    def install(self, package: McpPackage, source: str = "manual") -> Installation:
        return self.set(package, installed=True, enabled=True, source=source)

    def enable(self, package: McpPackage) -> Installation:
        if not self.effective(package).installed:
            raise PackageError(f"包尚未安装：{package.id}")
        return self.set(package, enabled=True)

    def disable(self, package: McpPackage) -> Installation:
        if not self.effective(package).installed:
            raise PackageError(f"包尚未安装：{package.id}")
        return self.set(package, enabled=False)

    def uninstall(self, package: McpPackage) -> Installation:
        return self.set(package, installed=False, enabled=False)


class PackageRegistry:
    """Merge discovered manifests with installation state for readers."""

    def __init__(self, root: Path, state_root: Path | None = None):
        self.root = Path(root)
        self.packages = discover(self.root)
        self.installations = Installations(state_root) if state_root is not None else None

    def installation(self, package: McpPackage) -> Installation:
        if self.installations is not None:
            return self.installations.effective(package)
        installed = package.default_installation == "installed"
        return Installation(installed=installed, enabled=installed, source="default")

    def status(self, package: McpPackage) -> dict:
        installation = self.installation(package)
        return {"id": package.id, "title": package.title, "version": package.version,
                "contract": package.contract, "default": package.default_installation,
                "installed": installation.installed, "enabled": installation.enabled,
                "source": installation.source, "stale": installation.stale,
                "route": package.route, "mount": package.mount, "data_dir": package.data_dir,
                "manifest_digest": package.digest}

    def statuses(self) -> list[dict]:
        return [self.status(package) for package in self.packages.values()]

    def enabled_packages(self) -> list[McpPackage]:
        return [package for package in self.packages.values() if self.installation(package).enabled]

    def navigation(self) -> list[dict]:
        items = [item for package in self.enabled_packages()
                 if (item := package.navigation()) is not None]
        return sorted(items, key=lambda item: (item["order"], item["id"]))

    def frontend_modules(self) -> list[dict]:
        rows = []
        for package in self.enabled_packages():
            frontend = package.frontend
            if not frontend or frontend["mount"] != "spa-module":
                continue
            rows.append({"id": package.id, "route": frontend["route"], "entry": frontend["entry"],
                         "requires": list(frontend.get("requires", []))})
        return rows

    def static_mounts(self) -> list[dict]:
        rows = []
        for package in self.enabled_packages():
            frontend = package.frontend
            if not frontend or frontend["mount"] != "static-iframe":
                continue
            rows.append({"id": package.id, "route": frontend["route"], "assets": frontend["assets"],
                         "requires": list(frontend.get("requires", []))})
        return rows

    def summary(self) -> dict:
        return {"root": str(self.root), "count": len(self.packages),
                "skipped": skipped(self.root), "packages": self.statuses()}
