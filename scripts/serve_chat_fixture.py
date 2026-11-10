"""Isolated browser-test backend. Uses fake CLI; never calls a model."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

from agent_base.config import Config
from agent_base.repositories import Repositories
from agent_base.web import create_app

project = Path(__file__).resolve().parents[1]
root = project / ".agent-base/ui-fixture"
root.mkdir(parents=True, exist_ok=True)
root = Path(tempfile.mkdtemp(prefix="session-", dir=root))
file = root / "config.toml"
file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\nengine="dsh"\ncommand='
    + json.dumps([sys.executable, str(project / "tests/fixtures/fake_dsh.py")])
    + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=20\n')
config = Config.load(file)
repositories = Repositories(config)
try:
    repo = root / "finance"
    shutil.copytree(project / "examples/repo-resources/finance", repo, ignore=shutil.ignore_patterns("__pycache__"))
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "UI fixture"], check=True)
    repositories.install(str(repo))
    app = create_app(config)
    @app.post("/fixture/fire/{name}")
    def fire_fixture_schedule(name: str):
        # Deterministic clock hook only in this fake-CLI test server, never in production.
        app.state.scheduler.work(name)
        with app.state.scheduler.store.connection() as conn:
            conn.execute("UPDATE works SET next_due=? WHERE id=?", (time.time()-1, name))
        app.state.scheduler.cron_tick()
        return {"dispatched": app.state.scheduler.dispatch()}
    uvicorn.run(app, host="127.0.0.1", port=8879, log_level="warning")
finally:
    for item in repositories.list():
        repositories.stop(item["id"])
