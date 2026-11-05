"""Materialize three independent Git repos from the source examples."""
import json
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / ".demo" / "resources"
repos = []
for example in sorted((root / "examples/repo-resources").iterdir()):
    if not (example / "Installation.md").is_file():
        continue
    target = output / example.name
    if not target.exists():
        shutil.copytree(example, target, ignore=shutil.ignore_patterns("__pycache__"))
        for directory, filename in (("mcp", "domain_server.py"), ("tools", "write_report.py"), ("tools", "verify.py"), ("tools", "initialize.py")):
            (target / directory).mkdir(exist_ok=True)
            shutil.copyfile(root / "examples/repo-resources/_shared" / filename, target / directory / filename)
        subprocess.run(["git", "init", "--quiet", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "add", "."], check=True)
        subprocess.run(["git", "-C", str(target), "-c", "user.name=Agent Base Demo", "-c", "user.email=demo@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "Repo resources demo"], check=True)
    repos.append(str(target))
print(json.dumps({"repos": repos}, ensure_ascii=False, indent=2))
