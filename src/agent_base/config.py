from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    path: Path
    data: Path
    runtime: dict = field(default_factory=dict)
    max_concurrent: int = 2

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
        validate_selection(runtime)
        root = Path(raw.get("storage", {}).get("root", ".agent-base")).expanduser()
        data = (root if root.is_absolute() else path.parent / root).resolve()
        cap = raw.get("queue", {}).get("max_concurrent", 2)
        if not isinstance(cap, int) or not 1 <= cap <= 32:
            raise ValueError("max_concurrent 必须是 1–32 的整数")
        timeout = runtime.get("timeout_seconds", 1800)
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise ValueError("timeout_seconds 必须在 1–86400 之间")
        return cls(path, data, runtime, cap)


def validate_selection(runtime: dict):
    model = runtime.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}", model):
        raise ValueError("model 必须是有效模型 ID，不得包含空格或 shell 字符")
    effort = runtime.get("effort")
    if effort is not None and (not isinstance(effort, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", effort)):
        raise ValueError("effort 必须是有效档位名")
    if runtime.get("engine") == "codeagent" and effort not in {"low", "medium", "high", "max"}:
        raise ValueError("CodeAgent effort 只支持 low/medium/high/max")


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
        '# CodeAgent: 设置 engine/command/model，并按内网实际版本调整 capabilities。\n'
        '# config_dir = "C:/Users/me/.cac"\n'
        '# max_concurrent 建议先改为 1，验证共享账号并发后再增加。\n', encoding="utf-8")
    return path
