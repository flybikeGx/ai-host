import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_base.conversations import ConversationBusy, Conversations
from agent_base.processes import alive
from agent_base.web import create_app


def finish(chats, turn_id, seconds=20):
    until = time.time() + seconds
    while time.time() < until:
        chats.reconcile()
        turn = chats.turn(turn_id)
        if turn["status"] not in {"queued", "running"}:
            return turn
        time.sleep(0.1)
    raise AssertionError(chats.turn(turn_id))


def test_admin_conversation_native_session_and_persistence(config):
    chats = Conversations(config)
    chat = chats.create("admin", model="another-model", effort="low")
    first = chats.send(chat["id"], "first 中文 & $(literal)", "message-1")
    assert finish(chats, first["id"])["status"] == "succeeded"
    session = chats.get(chat["id"])["native_id"]
    second = chats.send(chat["id"], "follow-up", "message-2")
    assert finish(chats, second["id"])["status"] == "succeeded"
    root = config.data / "conversations" / chat["id"]
    captured = json.loads((root / "workspace/captured-input.json").read_text())
    assert captured["argv"][captured["argv"].index("--session-id") + 1] == session
    assert captured["home"] == str(root / "dsh-home")
    assert captured["selection"]["model"] == "another-model" and captured["selection"]["reasoningEffort"] == "low"
    snapshot = Conversations(config).snapshot(chat["id"])
    assert len(snapshot["turns"]) == 2 and snapshot["turns"][0]["user_text"] == "first 中文 & $(literal)"
    assert snapshot["turns"][1]["assistant_text"] == "fixture completed"
    assert snapshot["conversation"]["native_id"] == session
    assert chats.list()[0]["title"] == "first 中文 & $(literal)"


def test_idempotency_and_one_active_turn_per_conversation(config):
    chats = Conversations(config)
    chat = chats.create()
    with ThreadPoolExecutor(max_workers=3) as pool:
        answers = list(pool.map(lambda _: Conversations(config).send(chat["id"], "FIXTURE_SLEEP", "same-request"), range(3)))
    assert len({a["id"] for a in answers}) == 1
    assert sum(not a["duplicate"] for a in answers) == 1
    with pytest.raises(ConversationBusy):
        chats.send(chat["id"], "another message", "different-request")
    with pytest.raises(ValueError, match="不同内容"):
        chats.send(chat["id"], "different text", "same-request")
    chats.cancel(chat["id"])
    assert finish(chats, answers[0]["id"])["status"] == "cancelled"


def test_separate_conversations_have_separate_sessions_and_workspaces(config):
    chats = Conversations(config)
    one, two = chats.create(), chats.create()
    turns = [chats.send(one["id"], "hello", "1"), chats.send(two["id"], "hello", "1")]
    assert all(finish(chats, t["id"])["status"] == "succeeded" for t in turns)
    assert chats.get(one["id"])["native_id"] != chats.get(two["id"])["native_id"]
    for chat in (one, two):
        assert (config.data / "conversations" / chat["id"] / "workspace/captured-input.json").exists()


def test_cancel_and_timeout_preserve_partial_session(config):
    chats = Conversations(config)
    config.runtime["timeout_seconds"] = 1.5
    chat = chats.create()
    turn = chats.send(chat["id"], "FIXTURE_SLEEP", "1")
    final = finish(chats, turn["id"])
    assert final["status"] == "timed_out"
    assert chats.get(chat["id"])["native_id"]
    assert final["child_pid"] is None
    assert chats.snapshot(chat["id"])["events"][-1]["data"]["status"] == "timed_out"


def test_repo_chat_reuses_resources_and_workspace(config, repositories, resource_repos):
    item = repositories.install(str(resource_repos["finance"]))
    original = repositories.health(item)
    chats = Conversations(config)
    chat = chats.create(item["id"])
    one = chats.send(chat["id"], "run example", "first")
    first = finish(chats, one["id"])
    assert first["status"] == "succeeded", first
    workspace = config.data / "conversations" / chat["id"] / "workspace"
    before = json.loads((workspace / "artifacts/report.json").read_text())
    skill = workspace / ".agent/skills/analysis/SKILL.md"
    inode = skill.stat().st_ino
    session = chats.get(chat["id"])["native_id"]
    (workspace / "remember.txt").write_text("conversation state")
    two = chats.send(chat["id"], "follow up", "second")
    second = finish(chats, two["id"])
    assert second["status"] == "succeeded", second
    after = json.loads((workspace / "artifacts/report.json").read_text())
    assert before["analysis"]["pid"] == after["analysis"]["pid"]
    assert before["analysis"]["boot_id"] == after["analysis"]["boot_id"]
    assert after["analysis"]["call_number"] == before["analysis"]["call_number"] + 1
    assert skill.stat().st_ino == inode and (workspace / "remember.txt").read_text() == "conversation state"
    assert chats.get(chat["id"])["native_id"] == session
    assert repositories.health(repositories.get(item["id"]))["boot_id"] == original["boot_id"]
    assert chats.store.get(second["run_id"])["result"]["verification"] == "conversation_only"
    assert {e["type"] for e in chats.snapshot(chat["id"])["events"]} >= {"tool.call", "tool.result", "turn.finished"}


def test_failure_is_not_chat_success_and_does_not_replay(config):
    chats = Conversations(config)
    chat = chats.create()
    turn = chats.send(chat["id"], "FIXTURE_FAIL", "failure")
    assert finish(chats, turn["id"])["status"] == "failed"
    again = chats.send(chat["id"], "FIXTURE_FAIL", "failure")
    assert again["duplicate"] and again["id"] == turn["id"]
    assert len(chats.snapshot(chat["id"])["turns"]) == 1


def test_native_reasoning_is_not_exposed_as_chat_content(config):
    chats = Conversations(config)
    chat = chats.create()
    # A normal completed turn supplies a stable event stream for this check.
    turn = chats.send(chat["id"], "hello", "1")
    finish(chats, turn["id"])
    chats.native_event(turn["id"], chat["id"], None, {"type": "thinking", "text": "private reasoning"})
    assert "private reasoning" not in json.dumps(chats.snapshot(chat["id"]))
    chats.native_event(turn["id"], chat["id"], None, {"type": "tool_result", "callId": "failed-tool", "status": "completed", "result": '{"isError":true}'})
    event = chats.snapshot(chat["id"])["events"][-1]
    assert event["type"] == "tool.result" and event["data"]["status"] == "failed"


def test_web_history_event_replay_auth_and_archive(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "test-chat-token")
    app = create_app(config, background=False)
    headers = {"Authorization": "Bearer test-chat-token"}
    with TestClient(app) as client:
        assert client.get("/api/chat/targets").status_code == 401
        assert client.get("/api/chat/targets", headers=headers).json()["targets"][0]["id"] == "admin"
        chat = client.post("/api/conversations", headers=headers, json={"target": "admin"}).json()
        sent = client.post(f"/api/conversations/{chat['id']}/messages", headers=headers, json={"text": "hello", "client_key": "request-1"})
        assert sent.status_code == 202
        chats = Conversations(config)
        assert finish(chats, sent.json()["id"])["status"] == "succeeded"
        stream = client.get(f"/api/conversations/{chat['id']}/events?once=true", headers=headers)
        assert stream.headers["content-type"].startswith("text/event-stream")
        frames = [part for part in stream.text.split("\n\n") if "data: " in part]
        first_id = int(frames[0].splitlines()[0].split(": ")[1])
        replay = client.get(f"/api/conversations/{chat['id']}/events?once=true", headers={**headers, "Last-Event-ID": str(first_id)})
        assert f"id: {first_id}\n" not in replay.text
        history = client.get(f"/api/conversations/{chat['id']}", headers=headers).json()
        assert history["turns"][0]["assistant_text"] == "fixture completed"
        assert client.patch(f"/api/conversations/{chat['id']}", headers=headers, json={"title": "保留的会话", "archived": True}).status_code == 200
        assert client.get("/api/conversations", headers=headers).json() == []
        assert client.get("/api/conversations?archived=true", headers=headers).json()[0]["title"] == "保留的会话"
        assert client.post(f"/api/conversations/{chat['id']}/messages", headers=headers, json={"text": "hello", "client_key": "2"}).status_code == 400


def test_dead_chat_owner_becomes_interrupted(config, monkeypatch):
    chats = Conversations(config)
    chat = chats.create()
    monkeypatch.setattr("agent_base.conversations.subprocess.Popen", lambda *a, **k: type("Child", (), {"pid": 99999999})())
    turn = chats.send(chat["id"], "unknown operation", "1")
    with chats.store.connection() as conn:
        conn.execute("UPDATE conversation_turns SET updated=0 WHERE id=?", (turn["id"],))
    chats.reconcile()
    assert chats.turn(turn["id"])["status"] == "interrupted"
    assert chats.send(chat["id"], "unknown operation", "1")["duplicate"]


def test_chat_downloads_are_scoped_to_artifacts(config):
    chats = Conversations(config)
    chat = chats.create()
    workspace = config.data / "conversations" / chat["id"] / "workspace"
    (workspace / "artifacts").mkdir(parents=True)
    (workspace / "artifacts/report.txt").write_text("report content")
    (workspace / "outside.txt").write_text("not an artifact")
    (workspace / "artifacts/escape.txt").symlink_to(config.path)
    assert chats.files(chat["id"]) == [{"path": "artifacts/report.txt", "size": 14}]
    with TestClient(create_app(config, background=False)) as client:
        response = client.get(f"/api/conversations/{chat['id']}/download", params={"path": "artifacts/report.txt"})
        assert response.status_code == 200 and response.text == "report content"
        for path in ("outside.txt", "artifacts/../../config.toml", "artifacts/escape.txt"):
            assert client.get(f"/api/conversations/{chat['id']}/download", params={"path": path}).status_code == 400
