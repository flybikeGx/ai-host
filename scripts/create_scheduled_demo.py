"""Create a standalone local Git agent and a separate mutable input workspace."""
import json
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
demo = root / ".demo" / "scheduled"
repo = demo / "repo"
source = demo / "source"
if repo.exists():
    raise SystemExit(f"保留已有 demo：{demo}")
shutil.copytree(root / "examples/scheduled-agent", repo)
source.mkdir()
(source / "metrics.json").write_text(json.dumps([
    {"service": "api", "requests": 120, "errors": 3},
    {"service": "worker", "requests": 80, "errors": 1},
    {"service": "api", "requests": 30, "errors": 0}], indent=2))
subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
subprocess.run(["git", "-C", str(repo), "-c", "user.name=Agent Base Demo", "-c", "user.email=demo@example.invalid",
                "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "Scheduled metrics agent"], check=True)
print(json.dumps({"repo": str(repo), "workspace_source": str(source)}, indent=2))
