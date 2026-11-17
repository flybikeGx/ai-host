"""Materialize the managed-service and v3 agent demo Git repos from examples."""
import json
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / ".demo" / "services"
repos = []


def _git_init(target: Path):
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "-c", "user.name=Agent Base Demo", "-c", "user.email=demo@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "Managed service demo"], check=True)


def _copy(example: Path, target: Path):
    if target.exists():
        return
    shutil.copytree(example, target, ignore=shutil.ignore_patterns("__pycache__"))
    _git_init(target)


for group in ("ticket-center-stdio", "ticket-center-http"):
    source = root / "examples/managed-services" / group
    if source.is_dir():
        _copy(source, output / group)
        repos.append(str(output / group))

agents = root / "examples/managed-services/agents"
if agents.is_dir():
    for example in sorted(agents.iterdir()):
        if (example / "agent.yaml").is_file():
            _copy(example, output / "agents" / example.name)
            repos.append(str(output / "agents" / example.name))

print(json.dumps({"repos": repos}, ensure_ascii=False, indent=2))
