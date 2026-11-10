"""Portable prompt paths: instructions must refer to workspace-relative files."""
from __future__ import annotations

import re
from pathlib import Path


ABSOLUTE = re.compile(
    r"(?i)(?:file://[^\s`<>\"'）)]+|"
    r"(?<![a-z0-9_])[a-z]:[\\/][^\s`<>\"'）)]*|"
    r"\\\\[^\s\\/]+[\\/][^\s`<>\"'）)]*|"
    r"(?:~|\$HOME|\$\{HOME\}|%USERPROFILE%)[\\/][^\s`<>\"'）)]*|"
    r"(?<=[`\"'])/[^\s`<>\"'）),;，。；]+|"
    r"(?<![a-z0-9_.:/\\])/(?=[a-z0-9_.~-]|[^\s/`<>\"'）),;，。；]+/)[^\s`<>\"'）),;，。；]+)"
)
IGNORED = {".git", ".venv", "node_modules", ".agent-base", ".runtime", "__pycache__", ".agent", ".demo"}


def lint_text(text: str, file: str) -> list[dict]:
    issues = []
    for number, line in enumerate(text.splitlines(), 1):
        # Prompt wrappers such as </user_task> are markup, not file paths.
        line = re.sub(r"</[A-Za-z_][A-Za-z0-9_-]*\s*>", lambda match: " " * len(match.group()), line)
        for match in ABSOLUTE.finditer(line):
            issues.append({"file": file, "line": number, "column": match.start() + 1,
                           "path": match.group(), "message": "提示词必须使用相对路径"})
    return issues


def check_prompts(root: Path) -> list[dict]:
    root = root.resolve()
    issues = []
    files = [file for file in sorted(root.rglob("*.md")) if not any(part in IGNORED for part in file.relative_to(root).parts)]
    skill_roots = [file.parent for file in files if file.name == "SKILL.md"]
    for file in files:
        relative = file.relative_to(root)
        if any(part in IGNORED for part in relative.parts):
            continue
        if file.name not in {"AGENTS.md", "SKILL.md", "entrypoint.md"} and "skills" not in relative.parts and not any(file.is_relative_to(parent) for parent in skill_roots):
            continue
        issues.extend(lint_text(file.read_text(encoding="utf-8"), relative.as_posix()))
    return issues


def require_portable(root: Path):
    issues = check_prompts(root)
    if issues:
        raise ValueError("AGENTS/entrypoint/skill 包含绝对路径：" + "; ".join(
            f"{item['file']}:{item['line']} {item['path']}" for item in issues[:20]))
