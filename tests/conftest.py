import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_base.config import Config


@pytest.fixture
def config(tmp_path):
    fake = Path(__file__).parent / "fixtures" / "fake_dsh.py"
    file = tmp_path / "config.toml"
    file.write_text('[storage]\nroot="data"\n[queue]\nmax_concurrent=2\n[runtime]\nengine="dsh"\ncommand='
                    + json.dumps([sys.executable, str(fake)])
                    + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=15\n')
    configuration = Config.load(file)
    yield configuration
    from agent_base.knowledge_service import KnowledgeService
    if (configuration.data / 'knowledge/service.json').exists():
        KnowledgeService(configuration).stop()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "test repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("请读取输入并生成 artifacts/report.txt；必须包含中文标记：仓库规则。", encoding="utf-8")
    manifest = {"schema_version": 1, "task": {"default": "write report"},
                "completion": {"artifacts": [{"path": "artifacts/report.txt", "min_bytes": 1}]}}
    (root / "agent.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "fixture"], check=True)
    return root


import shutil
from agent_base.repositories import Repositories

@pytest.fixture
def resource_repos(tmp_path):
    examples = Path(__file__).resolve().parents[1] / "examples/repo-resources"
    result = {}
    for name in ("finance", "inventory", "research"):
        path = tmp_path / name
        shutil.copytree(examples / name, path, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", "--quiet", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "fixture"], check=True)
        result[name] = path
    return result


@pytest.fixture
def repositories(config):
    manager = Repositories(config)
    yield manager
    for item in manager.list():
        manager.stop(item["id"])
