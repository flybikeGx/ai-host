"""C3 read-path ownership: domain-scoped targets, per-owner reads, 404 isolation.

The frozen contract is docs/CHAT_MULTIUSER_DESIGN.md §5.1/§5.3/§5.4, §6.1-6.3,
§7.2-7.4 and §8.2.  These tests drive the real FastAPI app with real governance
accounts; they do not mock the resolver, the database or the route layer.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_base.conversations import Conversations
from agent_base.web import create_app

PASSWORD = "fixture-member-password"
ADMIN_PASSWORD = "fixture-browser-password"
SHARED_TOKEN = "fixture-legacy-token"
MISSING = "does-not-exist"


@pytest.fixture
def chat(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", SHARED_TOKEN)
    app = create_app(config, background=False)
    gov = app.state.governance
    default = gov.store.get_domain_by_code("default")
    other = gov.create_domain("other", "Other")
    users = {}
    for name, role, domain in (("alice", "developer", default), ("bob", "developer", default),
                               ("carol", "developer", other)):
        user = gov.create_user(name, password=PASSWORD, home_domain_id=domain["id"])
        gov.add_membership(user["id"], domain["id"])
        gov.grant_role(user["id"], gov.store.get_role_by_name(role)["id"], domain_id=domain["id"])
        users[name] = user
    # task.read without file.read must not expose the artifact catalog.
    reader_role = gov.create_role("task-reader", permissions=["agent.read", "task.read"])
    reader = gov.create_user("reader", password=PASSWORD, home_domain_id=default["id"])
    gov.add_membership(reader["id"], default["id"])
    gov.grant_role(reader["id"], reader_role["id"], domain_id=default["id"])
    users["reader"] = reader
    multi = gov.create_user("multi", password=PASSWORD, home_domain_id=default["id"])
    for domain in (default, other):
        gov.add_membership(multi["id"], domain["id"])
        gov.grant_role(multi["id"], gov.store.get_role_by_name("developer")["id"], domain_id=domain["id"])
    users["multi"] = multi
    service = gov.create_user("fixture-bot", user_type="service", home_domain_id=default["id"])
    gov.add_membership(service["id"], default["id"])
    gov.grant_role(service["id"], gov.store.get_role_by_name("platform_admin")["id"], domain_id=default["id"])
    users["service"] = service
    admin = gov.store.get_user_by_username("admin")
    gov.store.update_user(admin["id"], home_domain_id=default["id"])
    users["admin"] = admin
    state = dict(app=app, gov=gov, users=users, default=default, other=other,
                 admin_password=ADMIN_PASSWORD)
    state.update(
        alice_chat=make_chat(config, "alice-chat", users["alice"]["id"], default["id"], "alice"),
        bob_chat=make_chat(config, "bob-chat", users["bob"]["id"], default["id"], "bob"),
        carol_chat=make_chat(config, "carol-chat", users["carol"]["id"], other["id"], "carol"),
        reader_chat=make_chat(config, "reader-chat", reader["id"], default["id"], "reader"),
        multi_default_chat=make_chat(config, "multi-default-chat", multi["id"], default["id"], "multi-default"),
        multi_other_chat=make_chat(config, "multi-other-chat", multi["id"], other["id"], "multi-other"),
        orphan_chat=make_chat(config, "orphan-chat", None, default["id"], "orphan"),
    )
    with TestClient(app) as client:
        state["client"] = client
        yield state


def make_chat(config, chat_id, owner, domain, label):
    now = time.time()
    chats = Conversations(config)
    (config.data / "conversations" / chat_id / "workspace/artifacts").mkdir(parents=True)
    (config.data / "conversations" / chat_id / "workspace/artifacts/report.txt").write_text(label + " artifact\n")
    with chats.store.connection() as conn:
        conn.execute(
            "INSERT INTO conversations(id,title,target,spec,created,updated,owner_user_id,domain_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, chat_id, "admin", json.dumps({"runtime": {}, "auto_title": False}), now, now, owner, domain))
    return chat_id


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


def login(client, username, password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response


def bearer(client, username, password=PASSWORD):
    client.post("/api/auth/logout")
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def test_owner_isolation_on_list_snapshot_events_files_and_download(chat):
    client = chat["client"]
    login(client, "alice")
    listed = client.get("/api/conversations")
    assert listed.status_code == 200
    titles = {item["title"] for item in listed.json()}
    assert titles == {"alice-chat"}
    for suffix in ("", "/events?once=true", "/files", "/download?path=artifacts/report.txt"):
        assert client.get(f"/api/conversations/{chat['bob_chat']}{suffix}").status_code == 404
        assert client.get(f"/api/conversations/{chat['carol_chat']}{suffix}").status_code == 404
        assert client.get(f"/api/conversations/{chat['orphan_chat']}{suffix}").status_code == 404
    missing = client.get(f"/api/conversations/{MISSING}")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "对话不存在", "code": "conversation_not_found"}
    for suffix in ("/events?once=true", "/files", "/download?path=artifacts/report.txt"):
        assert client.get(f"/api/conversations/{MISSING}{suffix}").json()["detail"] == "对话不存在"
    assert client.get(f"/api/conversations/{chat['alice_chat']}").status_code == 200
    assert client.get(f"/api/conversations/{chat['alice_chat']}/files").json()[0]["path"] == "artifacts/report.txt"
    assert client.get(f"/api/conversations/{chat['alice_chat']}/download",
                      params={"path": "artifacts/report.txt"}).text == "alice artifact\n"
    missing_artifact = client.get(f"/api/conversations/{chat['alice_chat']}/download",
                                  params={"path": "artifacts/missing.txt"})
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["code"] == "artifact_not_found"

    login(client, "bob")
    assert {item["title"] for item in client.get("/api/conversations").json()} == {"bob-chat"}
    assert client.get(f"/api/conversations/{chat['alice_chat']}").status_code == 404
    assert client.get(f"/api/conversations/{chat['bob_chat']}").status_code == 200

    login(client, "carol")
    assert {item["title"] for item in client.get("/api/conversations").json()} == {"carol-chat"}
    assert client.get(f"/api/conversations/{chat['alice_chat']}").status_code == 404
    assert client.get(f"/api/conversations/{chat['carol_chat']}").status_code == 200


def test_platform_admin_scope_all_is_read_only_and_audited(chat):
    client, gov = chat["client"], chat["gov"]
    login(client, "admin", chat["admin_password"])
    assert chat["alice_chat"] not in {item["title"] for item in client.get("/api/conversations").json()}
    assert client.get(f"/api/conversations/{chat['alice_chat']}").status_code == 404
    listed = client.get("/api/conversations?scope=all")
    assert listed.status_code == 200
    assert {item["title"] for item in listed.json()} >= {"alice-chat", "bob-chat", "carol-chat", "reader-chat"}
    assert client.get(f"/api/conversations/{chat['alice_chat']}?scope=all").status_code == 200
    assert client.get(f"/api/conversations/{chat['alice_chat']}/events?scope=all&once=true").status_code == 200
    assert client.get(f"/api/conversations/{chat['alice_chat']}/files?scope=all").json()[0]["path"] == "artifacts/report.txt"
    download = client.get(f"/api/conversations/{chat['carol_chat']}/download",
                          params={"scope": "all", "path": "artifacts/report.txt"})
    assert download.status_code == 200 and download.text == "carol artifact\n"
    scope_actions = [row for row in gov.store.list_audit(user_id=chat["users"]["admin"]["id"])
                     if row["action"] == "chat.scope_all"]
    assert {row["detail"]["path"] for row in scope_actions} >= {
        "/api/conversations", "/api/conversations/{id}", "/api/conversations/{id}/events",
        "/api/conversations/{id}/files", "/api/conversations/{id}/download"}
    assert all(row["level"] == "chat" for row in scope_actions)

    login(client, "alice")
    assert client.get(f"/api/conversations/{chat['alice_chat']}?scope=all").status_code == 403
    assert client.get(f"/api/conversations?scope=all").status_code == 403
    assert client.get("/api/conversations?scope=everything").status_code == 400
    assert client.get(f"/api/conversations/{chat['alice_chat']}?scope=everything").status_code == 400
    denied = client.get("/api/conversations", params={"domain_id": chat["other"]["id"]})
    assert denied.status_code == 403 and denied.json()["code"] == "domain_forbidden"


def test_targets_are_domain_scoped_and_admin_hidden_from_members(chat, config):
    install_agent(config, "default-agent", chat["default"]["id"], "Default Agent")
    install_agent(config, "other-agent", chat["other"]["id"], "Other Agent")
    install_agent(config, "inherited-agent", None, "Inherited Agent")
    client = chat["client"]

    login(client, "alice")
    targets = client.get("/api/chat/targets")
    assert targets.status_code == 200
    ids = [item["id"] for item in targets.json()["targets"]]
    assert "default-agent" in ids and "inherited-agent" in ids  # NULL domain is the default domain
    assert "other-agent" not in ids and "admin" not in ids
    assert targets.json()["defaults"] == {"model": "fixture-model", "effort": "high"}

    login(client, "carol")
    ids = [item["id"] for item in client.get("/api/chat/targets").json()["targets"]]
    assert "other-agent" in ids and "default-agent" not in ids

    login(client, "admin", chat["admin_password"])
    ids = [item["id"] for item in client.get("/api/chat/targets").json()["targets"]]
    assert ids[0] == "admin" and "default-agent" in ids and "other-agent" not in ids
    ids = [item["id"] for item in client.get("/api/chat/targets",
                                            headers={"x-domain-id": chat["other"]["id"]}).json()["targets"]]
    assert "admin" in ids and "other-agent" in ids and "default-agent" not in ids

    service = chat["gov"].login_with_api_key(chat["users"]["service"]["api_key"])["token"]
    headers = {"Authorization": "Bearer " + service}
    denied = client.get("/api/chat/targets", headers=headers)
    assert denied.status_code == 403 and denied.json()["code"] == "service_account_forbidden"
    assert client.get("/api/conversations", headers=headers).status_code == 403


def test_list_defaults_across_selectable_domains(chat):
    client = chat["client"]
    login(client, "multi")
    titles = {item["title"] for item in client.get("/api/conversations").json()}
    assert titles == {"multi-default-chat", "multi-other-chat"}
    filtered = {item["title"] for item in client.get("/api/conversations",
                                                    params={"domain_id": chat["other"]["id"]}).json()}
    assert filtered == {"multi-other-chat"}
    denied = client.get("/api/conversations", params={"domain_id": "missing"})
    assert denied.status_code == 403 and denied.json()["code"] == "domain_forbidden"
    assert client.get(f"/api/conversations/{chat['multi_other_chat']}").status_code == 200
    assert client.get(f"/api/conversations/{chat['multi_default_chat']}").status_code == 200


def test_null_owner_is_visible_only_to_bootstrap_and_admin_scope_all(chat):
    client = chat["client"]
    login(client, "alice")
    assert client.get(f"/api/conversations/{chat['orphan_chat']}").status_code == 404
    assert "orphan-chat" not in {item["title"] for item in client.get("/api/conversations").json()}
    login(client, "admin", chat["admin_password"])
    assert client.get(f"/api/conversations/{chat['orphan_chat']}").status_code == 404
    # §5.3 orders the NULL-owner check before the scope=all bypass, so a NULL
    # owner stays invisible even to an explicit admin scope=all read.
    assert client.get(f"/api/conversations/{chat['orphan_chat']}?scope=all").status_code == 404
    client.post("/api/auth/logout")
    headers = {"Authorization": "Bearer " + SHARED_TOKEN}
    assert client.get(f"/api/conversations/{chat['orphan_chat']}", headers=headers).status_code == 200
    assert "orphan-chat" in {item["title"] for item in client.get("/api/conversations", headers=headers).json()}


def test_snapshot_files_field_collapses_without_file_read(chat):
    client = chat["client"]
    login(client, "reader")
    snapshot = client.get(f"/api/conversations/{chat['reader_chat']}")
    assert snapshot.status_code == 200 and snapshot.json()["files"] == []
    denied = client.get(f"/api/conversations/{chat['reader_chat']}/files")
    assert denied.status_code == 403 and denied.json()["code"] == "permission_denied"
    assert client.get(f"/api/conversations/{chat['reader_chat']}/download",
                      params={"path": "artifacts/report.txt"}).status_code == 403
    login(client, "alice")
    assert client.get(f"/api/conversations/{chat['alice_chat']}").json()["files"]
