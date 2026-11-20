from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


PROJECT = Path(__file__).resolve().parents[2]

# Route keys already owned by the dsh DeepSeek adapters; registering them on
# the pi-ai adapter would fail plugin load with DUPLICATE_ADAPTER.
RESERVED_PROVIDERS = {"deepseek-official", "deepseek-account"}
PROVIDER_APIS = {"openai-completions", "openai-responses", "anthropic-messages"}
REASONING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
PROVIDER_FIELDS = {"adapter", "api", "base_url", "api_key_env", "display_name",
                   "models", "default_context_window", "default_max_tokens"}
MODEL_FIELDS = {"id", "name", "context_window", "max_tokens", "input", "reasoning_efforts"}


def _positive_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return value


def parse_providers(raw) -> dict:
    """Validate provider definitions; credentials are env names, never values."""
    if not isinstance(raw, dict):
        raise ValueError("providers 必须是 [providers.<name>] 表")
    result = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError(f"provider 名必须是路由标识（小写字母/数字/连字符）：{name!r}")
        if name in RESERVED_PROVIDERS:
            raise ValueError(f"provider 名与内置路由冲突：{name}")
        if not isinstance(definition, dict):
            raise ValueError(f"providers.{name} 必须是表")
        unknown = set(definition) - PROVIDER_FIELDS
        if unknown:
            raise ValueError(f"providers.{name} 包含不支持的字段：{sorted(unknown)}")
        adapter = definition.get("adapter", "pi-ai")
        if adapter != "pi-ai":
            raise ValueError(f"providers.{name}.adapter 目前只支持 pi-ai 适配器")
        api = definition.get("api")
        if api not in PROVIDER_APIS:
            raise ValueError(f"providers.{name}.api 必须是 {sorted(PROVIDER_APIS)} 之一")
        base_url = definition.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError(f"providers.{name}.base_url 必须是 HTTP(S) 端点")
        parsed = urlparse(base_url)
        if parsed.username or parsed.query or parsed.fragment or not parsed.netloc:
            raise ValueError(f"providers.{name}.base_url 不得包含凭据、查询或片段")
        api_key_env = definition.get("api_key_env")
        if api_key_env is not None and (not isinstance(api_key_env, str)
                                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env)):
            raise ValueError(f"providers.{name}.api_key_env 必须是环境变量名（只引用，不写值）")
        display_name = definition.get("display_name")
        if display_name is not None and (not isinstance(display_name, str) or not 1 <= len(display_name) <= 64):
            raise ValueError(f"providers.{name}.display_name 须为 1–64 个字符")
        models = definition.get("models")
        if not isinstance(models, list) or not models:
            raise ValueError(f"providers.{name}.models 必须是非空模型目录")
        cleaned_models = []
        seen = set()
        for entry in models:
            if not isinstance(entry, dict):
                raise ValueError(f"providers.{name}.models 条目必须是表")
            unknown = set(entry) - MODEL_FIELDS
            if unknown:
                raise ValueError(f"providers.{name}.models 条目包含不支持的字段：{sorted(unknown)}")
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
                raise ValueError(f"providers.{name}.models 需要唯一非空 id")
            seen.add(model_id)
            model = {"id": model_id}
            if entry.get("name") is not None:
                if not isinstance(entry["name"], str) or not entry["name"].strip():
                    raise ValueError(f"providers.{name}.models.{model_id}.name 须为非空文本")
                model["name"] = entry["name"]
            for key in ("context_window", "max_tokens"):
                if entry.get(key) is not None:
                    model[key] = _positive_int(entry[key], f"providers.{name}.models.{model_id}.{key}")
            if entry.get("input") is not None:
                values = entry["input"]
                if not isinstance(values, list) or not values or not all(v in {"text", "image"} for v in values):
                    raise ValueError(f"providers.{name}.models.{model_id}.input 只能是 text/image 数组")
                model["input"] = values
            efforts = entry.get("reasoning_efforts")
            if efforts is not None:
                if efforts is False:
                    model["reasoning_efforts"] = False
                elif isinstance(efforts, dict) and efforts and all(
                        level in REASONING_LEVELS and isinstance(spelling, str) and spelling
                        for level, spelling in efforts.items()):
                    model["reasoning_efforts"] = dict(efforts)
                else:
                    raise ValueError(f"providers.{name}.models.{model_id}.reasoning_efforts 须为 false 或档位映射")
            cleaned_models.append(model)
        provider = {"api": api, "base_url": base_url, "models": cleaned_models}
        for key in ("api_key_env", "display_name", "default_context_window", "default_max_tokens"):
            if definition.get(key) is not None:
                provider[key] = definition[key]
        if provider.get("default_context_window") is not None:
            _positive_int(provider["default_context_window"], f"providers.{name}.default_context_window")
        if provider.get("default_max_tokens") is not None:
            _positive_int(provider["default_max_tokens"], f"providers.{name}.default_max_tokens")
        result[name] = provider
    return result


@dataclass
class Config:
    path: Path
    data: Path
    runtime: dict = field(default_factory=dict)
    max_concurrent: int = 2
    guardian: dict = field(default_factory=dict)
    oss: dict = field(default_factory=dict)
    web: dict = field(default_factory=dict)

    def web_auth_mode(self) -> str:
        mode = os.environ.get("AGENT_BASE_WEB_AUTH", self.web.get("auth", "hybrid"))
        if not isinstance(mode, str) or mode not in {"hybrid", "jwt"}:
            raise ValueError("web.auth 必须是 hybrid / jwt")
        return mode

    @classmethod
    def load(cls, filename: str | Path | None = None) -> "Config":
        filename = filename or os.environ.get("AGENT_BASE_CONFIG") or PROJECT / "agent-base.toml"
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"配置不存在：{path}。先运行 agent-base init。")
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        runtime = raw.get("runtime", {})
        if runtime.get("engine", "dsh") not in {"dsh", "codeagent"}:
            raise ValueError("runtime.engine 只支持 dsh / codeagent")
        command = runtime.get("command", [])
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError("runtime.command 必须是非空 argv 数组")
        providers = parse_providers(raw.get("providers", {}))
        runtime["providers"] = providers
        validate_selection(runtime)
        root = Path(raw.get("storage", {}).get("root", ".agent-base")).expanduser()
        data = (root if root.is_absolute() else path.parent / root).resolve()
        cap = raw.get("queue", {}).get("max_concurrent", 2)
        if not isinstance(cap, int) or not 1 <= cap <= 32:
            raise ValueError("max_concurrent 必须是 1–32 的整数")
        timeout = runtime.get("timeout_seconds", 1800)
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise ValueError("timeout_seconds 必须在 1–86400 之间")
        guardian = _parse_guardian(raw.get("guardian", {}))
        oss = _parse_oss(raw.get("oss", {}))
        web = raw.get("web", {})
        if not isinstance(web, dict):
            raise ValueError("web 必须是表")
        result = cls(path, data, runtime, cap, guardian, oss=oss, web=web)
        result.web_auth_mode()
        return result


def _parse_guardian(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("guardian 必须是表")
    unknown = set(raw) - {"enabled", "interval_seconds", "model", "effort", "timeout_seconds"}
    if unknown:
        raise ValueError(f"guardian 包含不支持的字段：{sorted(unknown)}")
    result = {"enabled": bool(raw.get("enabled", False)), "interval_seconds": raw.get("interval_seconds", 300)}
    if not isinstance(result["interval_seconds"], (int, float)) or not 30 <= result["interval_seconds"] <= 86400:
        raise ValueError("guardian.interval_seconds 必须在 30–86400 之间")
    for key in ("model", "effort"):
        if raw.get(key) is not None:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"guardian.{key} 必须是非空文本")
            result[key] = raw[key]
    if raw.get("timeout_seconds") is not None:
        if not isinstance(raw["timeout_seconds"], (int, float)) or not 60 <= raw["timeout_seconds"] <= 86400:
            raise ValueError("guardian.timeout_seconds 必须在 60–86400 之间")
        result["timeout_seconds"] = raw["timeout_seconds"]
    return result


def _parse_oss(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("oss 必须是表")
    unknown = set(raw) - {"endpoint"}
    if unknown:
        raise ValueError(f"oss 包含不支持的字段：{sorted(unknown)}")
    endpoint = os.environ.get("AGENT_BASE_OSS_ENDPOINT") or raw.get("endpoint")
    if endpoint is None:
        return {}
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        raise ValueError("oss.endpoint 必须是 HTTP(S) 端点")
    parsed = urlparse(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.netloc:
        raise ValueError("oss.endpoint 不得包含凭据、查询或片段")
    return {"endpoint": endpoint.rstrip("/")}


def validate_selection(runtime: dict):
    model = runtime.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}", model):
        raise ValueError("model 必须是有效模型 ID，不得包含空格或 shell 字符")
    effort = runtime.get("effort")
    if effort is not None and (not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", effort)):
        raise ValueError("effort 必须是有效档位名")
    if runtime.get("engine") == "codeagent" and effort not in {"low", "medium", "high", "max"}:
        raise ValueError("CodeAgent effort 只支持 low/medium/high/max")
    validate_provider_selection(runtime)


def validate_provider_selection(runtime: dict):
    """Custom providers fail closed: no fallback route, model, or effort."""
    provider = runtime.get("provider", "deepseek-official")
    providers = runtime.get("providers") or {}
    if provider in RESERVED_PROVIDERS:
        # Built-in DeepSeek routes; the CLI passes model ids through unchanged and
        # resolves (or reports) its own credential.
        return
    if provider not in providers:
        raise ValueError(f"未定义 provider：{provider}。在 [providers.{provider}] 定义或改用 deepseek-official，不会自动回退")
    entry = next((m for m in providers[provider]["models"] if m["id"] == runtime.get("model")), None)
    if entry is None:
        raise ValueError(f"模型 {runtime.get('model')} 不在 provider {provider} 的模型目录中")
    effort = runtime.get("effort")
    if effort is not None:
        efforts = entry.get("reasoning_efforts")
        if efforts is False or not isinstance(efforts, dict) or effort not in efforts:
            raise ValueError(f"模型 {runtime.get('model')} 未声明推理档位 {effort}（providers.{provider} 模型目录）")


def initialize(path: Path, *, engine: str = "dsh", command: str | None = None) -> Path:
    import json

    path = path.expanduser().resolve()
    if path.exists():
        raise ValueError(f"保留现有配置：{path}")
    local = PROJECT / ".runtime" / "dsh" / "node_modules" / ".bin" / ("dsh.cmd" if os.name == "nt" else "dsh")
    executable = command or (str(local) if engine == "dsh" and local.exists() else shutil.which(engine) or engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[storage]\nroot = ".agent-base"\n\n[queue]\nmax_concurrent = 2\n\n[runtime]\n'
        f'engine = {json.dumps(engine)}\ncommand = {json.dumps([executable])}\n'
        'provider = "deepseek-official"\nmodel = "deepseek-flash"\neffort = "high"\n'
        'timeout_seconds = 1800\npermission_mode = "workspace-write"\n'
        'approval_policy = "never"\n\n'
        '# 内部网关示例（凭据只引用环境变量名，不写入本文件）：\n'
        '# [providers.internal-gateway]\n'
        '# api = "openai-completions"\n'
        '# base_url = "https://llm.internal.example/v1"\n'
        '# api_key_env = "INTERNAL_GATEWAY_API_KEY"\n'
        '# [[providers.internal-gateway.models]]\n'
        '# id = "internal-flash"\n'
        '# context_window = 131072\n'
        '# [providers.internal-gateway.models.reasoning_efforts]\n'
        '# high = "high"\n'
        '# 切换：runtime.provider = "internal-gateway"，model 填目录内 id。\n\n'
        '# CodeAgent: 设置 engine/command/model，并按内网实际版本调整 capabilities。\n'
        '# config_dir = "C:/Users/me/.cac"\n'
        '# max_concurrent 建议先改为 1，验证共享账号并发后再增加。\n', encoding="utf-8")
    return path
