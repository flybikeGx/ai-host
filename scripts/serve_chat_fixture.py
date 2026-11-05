"""Isolated browser-test backend. Uses fake CLI; never calls a model."""
import json
import sys
from pathlib import Path

import uvicorn

from agent_base.config import Config
from agent_base.repositories import Repositories
from agent_base.web import create_app

project = Path(__file__).resolve().parents[1]
root = project / ".agent-base/ui-fixture"
root.mkdir(parents=True, exist_ok=True)
file = root / "config.toml"
file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\nengine="dsh"\ncommand='
    + json.dumps([sys.executable, str(project / "tests/fixtures/fake_dsh.py")])
    + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=20\n')
config = Config.load(file)
repositories = Repositories(config)
try:
    repositories.install(str(project / ".demo/resources/finance"))
    uvicorn.run(create_app(config), host="127.0.0.1", port=8879, log_level="warning")
finally:
    for item in repositories.list():
        repositories.stop(item["id"])
