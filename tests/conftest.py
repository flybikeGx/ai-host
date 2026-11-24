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
    from agent_base.info_board_service import InfoBoardService
    if (configuration.data / 'info-board/service.json').exists():
        InfoBoardService(configuration).stop()


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


@pytest.fixture
def service_repos(tmp_path):
    """Git repos built from the managed-service examples; isolated state."""
    import shutil as _shutil
    examples = Path(__file__).resolve().parents[1] / "examples/managed-services"
    result = {}
    for name in ("ticket-center-stdio", "ticket-center-http"):
        path = tmp_path / name
        _shutil.copytree(examples / name, path, ignore=_shutil.ignore_patterns("__pycache__"))
        subprocess.run(["git", "init", "--quiet", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "service fixture"], check=True)
        result[name] = path
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("triage-agent", "ops-agent"):
        path = agents / name
        _shutil.copytree(examples / "agents" / name, ignore=_shutil.ignore_patterns("__pycache__"), dst=path)
        subprocess.run(["git", "init", "--quiet", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "agent fixture"], check=True)
        result[name] = path
    return result


def pytest_runtest_teardown(item, nextitem):
    """Stop repo gateways created by a test even when the test forgets to.

    Runs before fixture finalizers so the per-test config is still available.
    """
    config = item.funcargs.get("config")
    if config is None:
        return
    try:
        from agent_base.repositories import Repositories
        manager = Repositories(config)
        for entry in manager.list():
            try:
                manager.stop(entry["id"])
            except (OSError, ValueError):
                pass
    except Exception:
        pass


@pytest.fixture
def managed(config, monkeypatch):
    from agent_base.service_install import ManagedServices
    monkeypatch.setenv("TICKET_SERVICE_AUTH", "fixture-ticket-secret")
    monkeypatch.setenv("TICKET_SERVICE_HTTP_AUTH", "Bearer fixture-http-secret")
    manager = ManagedServices(config)
    yield manager
    for deployment in manager.services.deployments():
        if deployment["mode"] in {"managed", "external"}:
            try:
                manager.stop(deployment["id"], reason="test-teardown", drain=False)
            except (OSError, ValueError):
                pass
