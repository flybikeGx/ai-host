"""Manifest-driven mounting: every execution entry gets the board, disable stops it."""
import json
import time
from pathlib import Path

import pytest
import yaml

from agent_base.config import PROJECT
from agent_base.engines import dsh_patch
from agent_base.mcp_packages import Installations, load_package
from agent_base.mcp_runtime import (PackageRuntime, attach_packages, issue_grant,
                                     verify_grant)

PACKAGE = load_package(PROJECT / "first_party/info_board")
ENV = "AGENT_BASE_MCP_INFO_BOARD_AUTH"


@pytest.fixture
def installed(config):
    Installations(config.data / "mcp").install(PACKAGE)
    yield PACKAGE
    runtime = PackageRuntime(config, PACKAGE)
    if runtime.metadata():
        runtime.stop()


def turn_dir(tmp_path, name="turn"):
    path = tmp_path / name
    path.mkdir()
    return path


def mounted(patch):
    rows = yaml.load(Path(patch).read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or []
    return [insert for row in rows for insert in row.get("insert", [])
            if insert.get("id") == "info-board"]


def test_every_entry_gets_the_same_manifest_driven_mount(config, installed, tmp_path):
    actors = [("admin", "admin", None),
              ("repo:fixture", "repo:fixture", {"schema_version": 3, "services": []})]
    for index, (actor, expected_author, manifest) in enumerate(actors):
        workspace = tmp_path / f"ws-{index}"
        workspace.mkdir()
        (workspace / ".agent").mkdir()
        (workspace / ".agent/resources.json").write_text('{"namespace": "fixture"}')
        patch = dsh_patch(config.runtime, turn_dir(tmp_path, f"turn-{index}"))
        env = {}
        note = attach_packages(config, patch=patch, env=env, actor=actor,
                               workspace=workspace, deadline=time.time() + 120)
        assert "第一方 MCP" in note and "info_create" in note
        rows = mounted(patch)
        assert len(rows) == 1
        assert rows[0]["config"]["serverName"] == "info_board"
        assert rows[0]["config"]["url"].startswith("http://127.0.0.1:")
        assert ENV in env and env[ENV].startswith("Bearer ")
        assert env[ENV] not in patch.read_text()
        grant = verify_grant(PackageRuntime(config, PACKAGE).control_token(),
                             env[ENV].removeprefix("Bearer "))
        assert grant == {"actor": expected_author, "kind": "agent"}
        catalog = json.loads((workspace / ".agent/resources.json").read_text())
        assert catalog["packages"][0]["id"] == "info-board"
        assert catalog["packages"][0]["tools"][0] == "info_create"
        # Idempotent for a second call in the same turn.
        assert attach_packages(config, patch=patch, env=env, actor=actor,
                               workspace=workspace) == ""
        assert len(mounted(patch)) == 1


def test_disabled_or_failing_package_never_blocks_a_run(config, installed, tmp_path, monkeypatch):
    patch = dsh_patch(config.runtime, turn_dir(tmp_path))
    before = patch.read_text()
    Installations(config.data / "mcp").disable(PACKAGE)
    assert attach_packages(config, patch=patch, env={}, actor="admin") == ""
    assert patch.read_text() == before

    Installations(config.data / "mcp").enable(PACKAGE)

    def down(self, explicit=False):
        raise ValueError("simulated startup failure")
    monkeypatch.setattr(PackageRuntime, "ensure", down)
    env = {}
    note = attach_packages(config, patch=patch, env=env, actor="admin")
    assert "不可用" in note and ENV not in env
    assert mounted(patch) == []


def test_background_run_receives_and_uses_the_board(config, installed, repo):
    from agent_base.service import Service
    service = Service(config)
    job = service.submit(str(repo), "FIXTURE_INFO")
    result = service.wait(job["id"], 60)
    assert result["status"] == "succeeded", result
    workspace = Path(result["workspace"])
    captured = json.loads((workspace / "captured-info.json").read_text())
    assert captured["created"] == captured["listed"]
    assert captured["body"].startswith("# 信息")
    assert captured["updated"] == "运行时信息（更新）"
    events = [json.loads(line) for line in (Path(result["root"]) / "runtime.jsonl").read_text().splitlines()]
    tools = {event["tool"] for event in events if event.get("type") == "tool_call"}
    assert {"mcp__info_board__info_create", "mcp__info_board__info_list",
            "mcp__info_board__info_get", "mcp__info_board__info_update",
            "mcp__info_board__info_delete"} <= tools


def test_web_conversation_and_admin_cli_receive_the_board(config, installed):
    from agent_base.conversations import Conversations
    chats = Conversations(config)
    chat = chats.create("admin")
    turn = chats.send(chat["id"], "FIXTURE_INFO", "info-board-chat")
    deadline = time.time() + 60
    while time.time() < deadline and chats.turn(turn["id"])["status"] in {"queued", "running"}:
        chats.reconcile()
        time.sleep(0.1)
    assert chats.turn(turn["id"])["status"] == "succeeded"
    workspace = config.data / "conversations" / chat["id"] / "workspace"
    prompt = json.loads((workspace / "captured-input.json").read_text())["prompt"]
    assert "第一方 MCP" in prompt and "info-board" in prompt
    assert json.loads((workspace / "captured-info.json").read_text())["author"] == "admin"

    from agent_base.admin import Admin
    admin = Admin(config, "fixture")
    assert admin.turn("FIXTURE_INFO") == "fixture completed"
    prompt = json.loads((admin.workspace / "captured-input.json").read_text())["prompt"]
    assert "第一方 MCP" in prompt
    assert json.loads((admin.workspace / "captured-info.json").read_text())["author"] == "admin"


def test_cli_install_disable_uninstall_lifecycle(config, tmp_path, monkeypatch, capsys):
    import sys as _sys
    from agent_base import cli
    from agent_base.mcp_runtime import PackageRuntime
    assert not Installations(config.data / "mcp").effective(PACKAGE).installed
    try:
        for action in ("install", "disable", "uninstall"):
            monkeypatch.setattr(_sys, "argv", ["agent-base", "--config", str(config.path),
                                               "mcp", action, "info-board"])
            cli.main()
        capsys.readouterr()
        state = Installations(config.data / "mcp").effective(PACKAGE)
        assert not state.installed and not state.enabled
    finally:
        PackageRuntime(config, PACKAGE).stop()
