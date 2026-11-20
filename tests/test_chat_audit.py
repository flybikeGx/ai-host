"""C5 chat audit: every frozen §10 action, trace linkage and no content leaks.

The tests drive the real FastAPI app, real governance accounts and the real
SQLite stores. The write-failure test injects a storage-level trigger rather
than monkeypatching the ledger, so it also proves the business route catches a
real audit insert error and still succeeds.
"""
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from agent_base.chat_audit import insert
from agent_base.conversations import Conversations
from agent_base.store import Store
from agent_base.web import create_app

PASSWORD = "fixture-member-password"
ADMIN_PASSWORD = "fixture-browser-password"
SHARED_TOKEN = "fixture-legacy-token"
SEND_SECRET = "SEND_SECRET_MUST_NOT_BE_AUDITED"
TITLE_SECRET = "TITLE_SECRET_MUST_NOT_BE_AUDITED"
RENAMED_SECRET = "RENAMED_SECRET_MUST_NOT_BE_AUDITED"
ARTIFACT_SECRET = "ARTIFACT_SECRET_MUST_NOT_BE_AUDITED"


@pytest.fixture
def audit(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", SHARED_TOKEN)
    app = create_app(config, background=False)
    gov = app.state.governance
    default = gov.store.get_domain_by_code("default")
    alice = gov.create_user("alice", password=PASSWORD, home_domain_id=default["id"])
    gov.add_membership(alice["id"], default["id"])
    gov.grant_role(alice["id"], gov.store.get_role_by_name("developer")["id"],
                   domain_id=default["id"])
    admin = gov.store.get_user_by_username("admin")
    gov.store.update_user(admin["id"], home_domain_id=default["id"])
    install_agent(config, "default-agent", default["id"], "Default Agent")
    with TestClient(app) as client:
        yield dict(app=app, gov=gov, client=client, config=config, default=default,
                   alice=alice, admin=admin)


def login(client, username, password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text


def install_agent(config, install_id, domain_id, name):
    root = config.data / "repos" / install_id
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "package": {"name": name},
                "task": {"default": name + " task"}, "skills": [], "mcp": {"servers": []}}
    with Conversations(config).store.connection() as conn:
        conn.execute(
            "INSERT INTO repo_installations(id,repo_id,origin,commit_sha,root,manifest,status,created,enabled,domain_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (install_id, install_id, "fixture", "0" * 40, str(root), json.dumps(manifest),
             "ready", time.time(), 1, domain_id))


def create_chat(client, title=TITLE_SECRET, target="default-agent"):
    response = client.post("/api/conversations", json={"target": target, "title": title})
    assert response.status_code == 201, response.text
    return response.json()


def add_artifact(config, chat_id):
    directory = config.data / "conversations" / chat_id / "workspace" / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notes.txt").write_text(ARTIFACT_SECRET + "\n", encoding="utf-8")


def wait_terminal(config, turn_id, seconds=20):
    chats = Conversations(config)
    deadline = time.time() + seconds
    while time.time() < deadline:
        chats.reconcile()
        turn = chats.turn(turn_id)
        if turn["status"] not in {"queued", "running"}:
            return turn
        time.sleep(0.05)
    raise AssertionError(chats.turn(turn_id))


def send_and_stop(client, config, chat_id, client_key="c5-audit"):
    sent = client.post(f"/api/conversations/{chat_id}/messages",
                       json={"text": "FIXTURE_SLEEP " + SEND_SECRET, "client_key": client_key})
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["id"]
    stopped = client.post(f"/api/conversations/{chat_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json() == {"requested": True, "turn_id": turn_id}
    wait_terminal(config, turn_id)
    return turn_id


def chat_rows(gov, user_id=None):
    return [row for row in gov.store.list_audit(user_id=user_id)
            if row["level"] == "chat"]


def only(rows, action):
    matches = [row for row in rows if row["action"] == action]
    assert len(matches) == 1, (action, matches)
    return matches[0]


def test_six_business_actions_land_with_frozen_shape(audit):
    client, gov, config = audit["client"], audit["gov"], audit["config"]
    login(client, "alice")
    chat = create_chat(client)
    chat_id = chat["id"]
    add_artifact(config, chat_id)

    assert client.patch(f"/api/conversations/{chat_id}",
                        json={"title": RENAMED_SECRET}).status_code == 200
    assert client.patch(f"/api/conversations/{chat_id}", json={"archived": True}).status_code == 200
    assert client.patch(f"/api/conversations/{chat_id}", json={"archived": False}).status_code == 200
    turn_id = send_and_stop(client, config, chat_id)
    downloaded = client.get(f"/api/conversations/{chat_id}/download",
                            params={"path": "artifacts/notes.txt"})
    assert downloaded.status_code == 200 and downloaded.text == ARTIFACT_SECRET + "\n"

    rows = chat_rows(gov, audit["alice"]["id"])
    assert sorted(row["action"] for row in rows) == [
        "chat.archive", "chat.archive", "chat.create", "chat.download",
        "chat.rename", "chat.send", "chat.stop"]
    for row in rows:
        assert row["level"] == "chat"
        assert row["user_id"] == audit["alice"]["id"]
        assert row["domain_id"] == audit["default"]["id"]
        assert row["detail"]["mode"] == "jwt"
        expected_trace = turn_id if row["action"] in {"chat.send", "chat.stop"} else None
        assert row["trace_id"] == expected_trace

    assert only(rows, "chat.create")["detail"] == {
        "conversation_id": chat_id, "target": "default-agent", "target_kind": "repo",
        "title_auto": False, "model": "fixture-model", "effort": "high", "mode": "jwt"}
    assert only(rows, "chat.send")["detail"] == {
        "conversation_id": chat_id, "turn_id": turn_id, "mode": "jwt"}
    assert only(rows, "chat.stop")["detail"] == {
        "conversation_id": chat_id, "turn_id": turn_id, "requested": True, "mode": "jwt"}
    assert only(rows, "chat.rename")["detail"] == {
        "conversation_id": chat_id, "fields": ["title"], "mode": "jwt"}
    assert only(rows, "chat.download")["detail"] == {
        "conversation_id": chat_id, "path": "artifacts/notes.txt",
        "size": len(ARTIFACT_SECRET + "\n"), "mode": "jwt"}
    archived = sorted(row["detail"]["archived"] for row in rows if row["action"] == "chat.archive")
    assert archived == [False, True]

    # §10: message text, title values, artifact content and credentials never
    # enter the ledger; only the frozen detail keys do.
    ledger = json.dumps(rows, ensure_ascii=False)
    for secret in (SEND_SECRET, TITLE_SECRET, RENAMED_SECRET, ARTIFACT_SECRET,
                   PASSWORD, ADMIN_PASSWORD, SHARED_TOKEN):
        assert secret not in ledger
    for row in rows:
        assert not ({"user_text", "assistant_text", "title", "token", "content"}
                    & set(row["detail"]))


def test_trace_and_user_filters_link_the_turn(audit):
    client, gov, config = audit["client"], audit["gov"], audit["config"]
    login(client, "alice")
    chat_id = create_chat(client)["id"]
    turn_id = send_and_stop(client, config, chat_id)

    login(client, "admin", ADMIN_PASSWORD)
    by_trace = client.get("/api/v1/audit", params={"trace_id": turn_id})
    assert by_trace.status_code == 200
    assert {row["action"] for row in by_trace.json()} == {"chat.send", "chat.stop"}
    assert all(row["trace_id"] == turn_id for row in by_trace.json())
    assert all(row["user_id"] == audit["alice"]["id"] for row in by_trace.json())

    by_user = client.get("/api/v1/audit", params={"user_id": audit["alice"]["id"]})
    assert by_user.status_code == 200
    assert {row["action"] for row in by_user.json()} >= {
        "chat.create", "chat.send", "chat.stop"}
    assert all(row["user_id"] == audit["alice"]["id"] for row in by_user.json())
    assert client.get("/api/v1/audit", params={"trace_id": "not-a-turn"}).json() == []

    # The conversation id is not a turn trace: chat.create carries trace_id=None.
    assert client.get("/api/v1/audit", params={"trace_id": chat_id}).json() == []


def test_scope_all_writes_one_row_per_access(audit):
    client, gov, config = audit["client"], audit["gov"], audit["config"]
    login(client, "alice")
    chat_id = create_chat(client)["id"]
    add_artifact(config, chat_id)

    login(client, "admin", ADMIN_PASSWORD)
    assert client.get("/api/conversations", params={"scope": "all"}).status_code == 200
    assert client.get(f"/api/conversations/{chat_id}", params={"scope": "all"}).status_code == 200
    assert client.get(f"/api/conversations/{chat_id}/events",
                      params={"scope": "all", "once": "true"}).status_code == 200
    assert client.get(f"/api/conversations/{chat_id}/files",
                      params={"scope": "all"}).status_code == 200
    assert client.get(f"/api/conversations/{chat_id}/download",
                      params={"scope": "all", "path": "artifacts/notes.txt"}).status_code == 200

    rows = [row for row in chat_rows(gov, audit["admin"]["id"])
            if row["action"] == "chat.scope_all"]
    assert len(rows) == 5
    assert {row["detail"]["path"] for row in rows} == {
        "/api/conversations", "/api/conversations/{id}",
        "/api/conversations/{id}/events", "/api/conversations/{id}/files",
        "/api/conversations/{id}/download"}
    for row in rows:
        assert row["level"] == "chat"
        assert row["trace_id"] is None
        assert row["detail"]["mode"] == "jwt"
        assert "path" in row["detail"]
    listed = next(row for row in rows if row["detail"]["path"] == "/api/conversations")
    assert listed["detail"]["conversation_id"] is None
    assert listed["detail"]["count"] == 1
    by_id = [row for row in rows if row["detail"]["conversation_id"] == chat_id]
    assert len(by_id) == 4
    assert all(row["domain_id"] == audit["default"]["id"] for row in by_id)


def test_migrate_audit_uses_the_frozen_shape(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    store = Store(config.data)
    with store.connection() as conn:
        conn.execute("INSERT INTO conversations(id,title,target,spec,created,updated)"
                     " VALUES('old','old','admin','{}',1,1)")
        conn.execute("INSERT INTO repo_installations(id,repo_id,origin,commit_sha,root,manifest,status,created)"
                     " VALUES('repo','r','local','sha',?,'{}','ready',1)",
                     (str(config.data / "legacy-repo"),))
    app = create_app(config, background=False)
    gov = app.state.governance.store
    rows = [row for row in gov.list_audit() if row["action"] == "chat.migrate"]
    assert len(rows) == 1
    row = rows[0]
    admin = gov.get_user_by_username("admin")
    domain = gov.get_domain_by_code("default")["id"]
    assert row["level"] == "chat"
    assert row["user_id"] is None and row["trace_id"] is None
    assert row["domain_id"] == domain
    assert row["detail"] == {
        "conversations_owner": 1, "conversations_domain": 1,
        "repo_installations_domain": 1, "bootstrap_admin_id": admin["id"],
        "default_domain_id": domain}


def test_writer_rejects_content_bearing_and_mistraced_rows(config):
    store = Store(config.data)
    with store.connection() as conn:
        with pytest.raises(ValueError, match="禁止字段"):
            insert(conn, action="chat.send", domain_id=None, user_id="u", trace_id="t",
                   detail={"conversation_id": "c", "turn_id": "t", "user_text": SEND_SECRET})
        with pytest.raises(ValueError, match="trace_id"):
            insert(conn, action="chat.send", domain_id=None, user_id="u", trace_id=None,
                   detail={"conversation_id": "c", "turn_id": "t"})
        with pytest.raises(ValueError, match="trace_id"):
            insert(conn, action="chat.create", domain_id=None, user_id="u", trace_id="t",
                   detail={"conversation_id": "c", "target": "admin", "target_kind": "admin",
                           "title_auto": True, "model": "m", "effort": "high"})
        with pytest.raises(ValueError, match="未知 chat 审计动作"):
            insert(conn, action="chat.other", domain_id=None, user_id="u", trace_id=None,
                   detail={})
        with pytest.raises(ValueError, match="path"):
            insert(conn, action="chat.scope_all", domain_id=None, user_id="u", trace_id=None,
                   detail={"path": "", "conversation_id": None, "count": 0})
        with pytest.raises(ValueError, match="count"):
            insert(conn, action="chat.scope_all", domain_id=None, user_id="u", trace_id=None,
                   detail={"path": "/api/conversations", "conversation_id": None, "count": -1})


def test_audit_storage_failure_does_not_block_chat_actions(audit, caplog):
    client, gov, config = audit["client"], audit["gov"], audit["config"]
    login(client, "alice")
    with gov.store.connection() as conn:
        conn.execute("CREATE TRIGGER reject_chat_audit BEFORE INSERT ON audit_log "
                     "WHEN NEW.level='chat' BEGIN SELECT RAISE(ABORT,'audit storage failed'); END")
    caplog.set_level(logging.ERROR, logger="agent_base.chat_audit")

    created = client.post("/api/conversations", json={"target": "default-agent", "title": TITLE_SECRET})
    assert created.status_code == 201, created.text
    chat_id = created.json()["id"]
    add_artifact(config, chat_id)
    assert client.patch(f"/api/conversations/{chat_id}",
                        json={"title": RENAMED_SECRET}).status_code == 200
    assert client.patch(f"/api/conversations/{chat_id}", json={"archived": True}).status_code == 200
    assert client.patch(f"/api/conversations/{chat_id}", json={"archived": False}).status_code == 200
    turn_id = send_and_stop(client, config, chat_id)
    assert client.get(f"/api/conversations/{chat_id}/download",
                      params={"path": "artifacts/notes.txt"}).status_code == 200

    login(client, "admin", ADMIN_PASSWORD)
    assert client.get("/api/conversations", params={"scope": "all"}).status_code == 200

    assert [row for row in gov.store.list_audit() if row["level"] == "chat"] == []
    assert "chat audit write failed" in caplog.text
