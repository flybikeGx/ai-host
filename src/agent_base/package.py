from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import jsonschema
import yaml


def within(root: Path, name: str, *, exists: bool = True) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError(f"需要包内相对路径：{name!r}")
    file = (root / name).resolve()
    if not file.is_relative_to(root.resolve()):
        raise ValueError(f"路径越界：{name}")
    if exists and not file.exists():
        raise ValueError(f"文件不存在：{name}")
    return file


def argv(value) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x and "\0" not in x for x in value):
        raise ValueError("argv 必须是非空字符串数组，不能是 shell 命令字符串")
    return value


def expand(values: list[str], package: Path, workspace: Path, python: str) -> list[str]:
    replacements = {"${package.root}": str(package), "${workspace.root}": str(workspace), "${runtime.python}": python}
    result = []
    for value in argv(values):
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        if "${" in value:
            raise ValueError(f"不支持的占位符：{value}")
        result.append(value)
    return result


def load_package(root: Path) -> dict:
    instructions = within(root, "AGENTS.md")
    if not instructions.is_file() or instructions.stat().st_size > 128 * 1024:
        raise ValueError("AGENTS.md 必须为不超过 128 KiB 的普通文件")
    file = root / "agent.yaml"
    docs = [p for p in (root / "Installation.md", root / "installation.md") if p.is_file()]
    if len(docs) > 1 and not docs[0].samefile(docs[1]):
        raise ValueError("只允许一个 Installation.md/installation.md")
    blocks = re.findall(r"^```agent-base-install\s*\n(.*?)^```\s*$", docs[0].read_text(encoding="utf-8"), re.M | re.S) if docs else []
    if len(blocks) > 1 or (file.exists() and blocks):
        raise ValueError("只能有一个机器配置来源：agent.yaml 或 Installation.md 中唯一的 agent-base-install 配置块")
    manifest = yaml.safe_load(file.read_text(encoding="utf-8") if file.exists() else blocks[0]) if file.exists() or blocks else {"schema_version": 1}
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {1, 2}:
        raise ValueError("安装清单必须是 schema_version: 1 或 2 的对象")
    if manifest["schema_version"] == 2:
        from .path_lint import require_portable
        require_portable(root)
    unknown = set(manifest) - {"schema_version", "package", "task", "environment", "installation", "tools", "mcp", "skills", "completion", "limits"}
    if unknown:
        raise ValueError(f"未知清单字段：{sorted(unknown)}")
    for section in ("package", "task", "environment", "installation", "tools", "mcp", "completion", "limits"):
        if section in manifest and not isinstance(manifest[section], dict):
            raise ValueError(f"{section} 必须是对象")
    allowed = {
        "package": {"name", "version"}, "task": {"default"},
        "environment": {"lockfile"}, "installation": {"steps"}, "tools": {"local"},
        "mcp": {"servers"}, "completion": {"artifacts", "checks"}, "limits": {"wall_timeout_seconds"}}
    for section, fields in allowed.items():
        if set(manifest.get(section, {})) - fields:
            raise ValueError(f"{section} 包含当前版本不支持的字段")
    for name in ("steps",):
        items = manifest.get("installation", {}).get(name, [])
        if not isinstance(items, list):
            raise ValueError("installation.steps 必须是数组")
        for item in items:
            argv(item.get("argv"))
    for item in manifest.get("completion", {}).get("checks", []):
        argv(item.get("argv"))
    for section, key in (("tools", "local"), ("mcp", "servers"), ("completion", "artifacts"), ("completion", "checks")):
        items = manifest.get(section, {}).get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError(f"{section}.{key} 必须是对象数组")
    if not isinstance(manifest.get("skills", []), list):
        raise ValueError("skills 必须是数组")
    names = set()
    for tool in manifest.get("tools", {}).get("local", []):
        name = tool.get("name", "")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,47}", name) or name in names:
            raise ValueError(f"无效或重复工具名：{name}")
        names.add(name)
        argv(tool.get("argv"))
        schema = tool.get("input_schema", {"type": "object"})
        if isinstance(schema, str):
            schema = json.loads(within(root, schema).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        tool["input_schema"] = schema
        if tool.get("output_schema"):
            output = tool["output_schema"]
            if isinstance(output, str):
                output = json.loads(within(root, output).read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator.check_schema(output)
            tool["output_schema"] = output
    for server in manifest.get("mcp", {}).get("servers", []):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", server.get("id", "")):
            raise ValueError("MCP server id 无效")
        if server.get("transport") not in {"stdio", "streamable_http"}:
            raise ValueError("MCP transport 必须为 stdio / streamable_http")
        if server["transport"] == "stdio":
            argv(server.get("argv"))
        elif not str(server.get("url", "")).startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("远程 MCP 需要 https URL（本机可使用 http）")
    for skill in manifest.get("skills", []):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", skill.get("name", "")):
            raise ValueError("skill name 必须为小写字母、数字和连字符")
        within(root, skill["path"] + "/SKILL.md")
    for artifact in manifest.get("completion", {}).get("artifacts", []):
        within(root, artifact["path"], exists=False)
    limits = [manifest.get("limits", {}).get("wall_timeout_seconds")]
    for items in (manifest.get("installation", {}).get("steps", []), manifest.get("completion", {}).get("checks", []),
                  manifest.get("tools", {}).get("local", []), manifest.get("mcp", {}).get("servers", [])):
        limits.extend(item.get("timeout_seconds") for item in items)
    if any(value is not None and (not isinstance(value, (int, float)) or not 0 < value <= 86400) for value in limits):
        raise ValueError("清单中的 timeout 必须在 0–86400 秒之间")
    lockfile = manifest.get("environment", {}).get("lockfile")
    if lockfile:
        if lockfile != "uv.lock":
            raise ValueError("首版环境安装支持 uv.lock + pyproject.toml")
        within(root, lockfile)
        within(root, "pyproject.toml")
    return manifest


def freeze(workspace: Path, target: Path) -> dict:
    shutil.copytree(workspace, target, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", ".env", "__pycache__"), symlinks=True)
    # Resolve declared paths against the frozen input too; external symlinks
    # cannot become executable entrypoints through a different base directory.
    manifest = load_package(target)
    (target / "resolved.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_prompt(package: Path, workspace: Path, manifest: dict, task: str) -> str:
    skills = []
    for skill in manifest.get("skills", []):
        file = within(package, skill["path"] + "/SKILL.md")
        skills.append(f"### {skill.get('name', skill['path'])}\n{file.read_text(encoding='utf-8')}")
    skill_text = "\n\n".join(skills)
    if len(skill_text.encode()) > 128 * 1024:
        raise ValueError("技能说明超过 128 KiB，请减少首版启动时加载的技能")
    return (
        "请完成下面的一次独立仓库任务。工具执行和任务内上下文由你自己的 agent 循环管理。\n"
        "不得自动启动另一个同样任务；如输入不足或能力不可用，明确报告。\n"
        "工作区为当前目录。任务文件和产物使用相对路径，固定工具资源由基座解析。\n"
        "<repository_instructions>\n" + (package / "AGENTS.md").read_text(encoding="utf-8") + "\n</repository_instructions>\n"
        + ("<skills>\n" + skill_text + "\n</skills>\n" if skills else "")
        + "<user_task>\n" + task + "\n</user_task>\n"
        "完成后说明生成的文件、验证结果和未完成项。不要声称没有执行过的检查已经通过。\n"
    )


def instruction_hash(package: Path) -> str:
    return hashlib.sha256((package / "AGENTS.md").read_bytes()).hexdigest()
