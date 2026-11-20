"""C4 per-user concurrency and quota (§9, frozen).

Turns are seeded directly as ``queued`` rows (with a future ``updated`` so the
reconciler leaves them alone) to exercise the aggregation without spawning
dozens of supervisors; the accepted send still goes through the real route and
supervisor, and the race test proves the check and insert share one
``BEGIN IMMEDIATE`` transaction.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_base.config import Config
from agent_base.conversations import Conversations
from agent_base.web import create_app

PASSWORD = "fixture-member-password"
ADMIN_PASSWORD = "fixture-browser-password"
SHARED_TOKEN = "fixture-legacy-token"


@pytest.fixture
def quota(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", SHARED_TOKEN)
    app = create_app(config, background=False)
    gov = app.state.governance
    default = gov.store.get_domain_by_code("default")
    users = {}
    for name in ("alice", "bob", "carol"):
        user = gov.create_user(name, password=PASSWORD, home_domain_id=default["id"])
        gov.add_membership(user["id"], default["id"])
        gov.grant_role(user["id"], gov.store.get_role_by_name("developer")["id"], domain_id=default["id"])
        users[name] = user
    with TestClient(app) as client:
        yield dict(app=app, gov=gov, client=client, users=users, default=default)


def login(client, username):
    """Establish the page cookie and return a Bearer header for thread tests."""
    page = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert page.status_code == 200, page.text
    token = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert token.status_code == 200, token.text
    return {"Authorization": "Bearer " + token.json()["token"]}


def new_chat(config, chat_id, owner, domain):
    """A conversation row owned by a real fixture user (target admin = no Run)."""
    now = time.time()
    chats = Conversations(config)
    (config.data / "conversations" / chat_id).mkdir(parents=True, exist_ok=True)
    with chats.store.connection() as conn:
        conn.execute(
            "INSERT INTO conversations(id,title,target,spec,created,updated,owner_user_id,domain_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, chat_id, "admin", json.dumps({"runtime": {}, "auto_title": False}),
             now, now, owner, domain))
    return chat_id


def seed_active(config, owner, domain, count, prefix):
    """Insert active turns without processes; updated in the future skips reconcile."""
    chats = Conversations(config)
    now = time.time() + 3600
    ids = []
    with chats.store.connection() as conn:
        for n in range(count):
            chat_id = f"{prefix}-{n}"
            turn_id = f"{prefix}-turn-{n}"
            conn.execute(
                "INSERT INTO conversations(id,title,target,spec,created,updated,owner_user_id,domain_id)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (chat_id, chat_id, "admin", "{}", now, now, owner, domain))
            conn.execute(
                "INSERT INTO conversation_turns(id,conversation_id,client_key,user_text,status,created,updated,root)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (turn_id, chat_id, "seed", "seed", "queued", now, now,
                 str(config.data / "conversations" / chat_id / "turns" / turn_id)))
            ids.append(chat_id)
    return ids


def wait_terminal(config, turn_id, seconds=20):
    chats = Conversations(config)
    until = time.time() + seconds
    while time.time() < until:
        chats.reconcile()
        turn = chats.turn(turn_id)
        if turn["status"] not in {"queued", "running"}:
            return turn
        time.sleep(0.05)
    raise AssertionError(chats.turn(turn_id))


def test_per_user_quota_counts_only_that_user_and_rejects_the_ninth(quota, config):
    client = quota["client"]
    bob_chat = new_chat(config, "bob-quota", quota["users"]["bob"]["id"], quota["default"]["id"])
    alice_chat = new_chat(config, "alice-nine", quota["users"]["alice"]["id"], quota["default"]["id"])
    login(client, "bob")
    seed_active(config, quota["users"]["alice"]["id"], quota["default"]["id"], 8, "alice-active")
    # Bob has his own bucket: alice's eight active turns do not block him.
    bob = client.post(f"/api/conversations/{bob_chat}/messages",
                      json={"text": "hello", "client_key": "bob-1"})
    assert bob.status_code == 202, bob.text
    wait_terminal(config, bob.json()["id"])

    login(client, "alice")
    denied = client.post(f"/api/conversations/{alice_chat}/messages",
                         json={"text": "hello", "client_key": "alice-9"})
    assert denied.status_code == 429
    assert denied.json()["code"] == "user_turn_quota"
    with Conversations(config).store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM conversation_turns WHERE status='queued'").fetchone()[0] == 8


def test_quota_check_and_insert_share_one_transaction(quota, config):
    client = quota["client"]
    headers = login(client, "alice")
    first = new_chat(config, "race-one", quota["users"]["alice"]["id"], quota["default"]["id"])
    second = new_chat(config, "race-two", quota["users"]["alice"]["id"], quota["default"]["id"])
    seed_active(config, quota["users"]["alice"]["id"], quota["default"]["id"], 7, "race-seed")

    def send(chat_id):
        return client.post(f"/api/conversations/{chat_id}/messages", headers=headers,
                           json={"text": "hello", "client_key": "race-" + chat_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, (first, second)))
    codes = sorted(result.status_code for result in results)
    assert codes == [202, 429], [result.text for result in results]
    rejected = next(result for result in results if result.status_code == 429)
    assert rejected.json()["code"] == "user_turn_quota"
    accepted = next(result for result in results if result.status_code == 202)
    client.post(f"/api/conversations/{accepted.json()['conversation_id']}/stop", headers=headers)
    wait_terminal(config, accepted.json()["id"])


def test_global_32_turn_cap_stays_the_409_fallback(quota, config):
    client = quota["client"]
    seed_active(config, quota["users"]["alice"]["id"], quota["default"]["id"], 8, "global-a")
    seed_active(config, quota["users"]["bob"]["id"], quota["default"]["id"], 24, "global-b")
    carol_chat = new_chat(config, "carol-global", quota["users"]["carol"]["id"], quota["default"]["id"])
    login(client, "carol")
    denied = client.post(f"/api/conversations/{carol_chat}/messages",
                         json={"text": "hello", "client_key": "global-1"})
    assert denied.status_code == 409
    assert denied.json()["code"] == "conversation_busy"


def test_per_user_stream_quota_is_enforced_per_identity(quota, config):
    client = quota["client"]
    chat_id = new_chat(config, "stream-quota", quota["users"]["alice"]["id"], quota["default"]["id"])
    headers = login(client, "alice")
    key = quota["users"]["alice"]["id"]
    limiter = quota["app"].state.chat_streams
    assert limiter.limit == 4
    for _ in range(4):
        assert limiter.acquire(key)
    assert not limiter.acquire(key)
    denied = client.get(f"/api/conversations/{chat_id}/events", headers=headers)
    assert denied.status_code == 429
    assert denied.json()["code"] == "user_stream_quota"
    for _ in range(4):
        limiter.release(key)
    assert limiter.count(key) == 0

    # A completed stream releases its slot; another user has a separate bucket.
    allowed = client.get(f"/api/conversations/{chat_id}/events?once=true", headers=headers)
    assert allowed.status_code == 200
    assert limiter.count(key) == 0
    bob_chat = new_chat(config, "bob-stream", quota["users"]["bob"]["id"], quota["default"]["id"])
    bob_headers = login(client, "bob")
    for _ in range(4):
        assert limiter.acquire(key)
    assert client.get(f"/api/conversations/{bob_chat}/events?once=true", headers=bob_headers).status_code == 200
    for _ in range(4):
        limiter.release(key)


def test_config_limits_validate_and_accept_env_overrides(tmp_path, monkeypatch):
    def write(extra=""):
        path = tmp_path / "quota.toml"
        path.write_text('[storage]\nroot="data"\n[runtime]\nengine="dsh"\ncommand=["dsh"]\n'
                        'model="fixture-model"\neffort="high"\n' + extra)
        return path

    config = Config.load(write("[web]\nper_user_max_turns=3\nper_user_max_streams=5\n"))
    assert (config.per_user_max_turns, config.per_user_max_streams) == (3, 5)
    monkeypatch.setenv("AGENT_BASE_WEB_PER_USER_TURNS", "7")
    monkeypatch.setenv("AGENT_BASE_WEB_PER_USER_STREAMS", "2")
    overridden = Config.load(write("[web]\nper_user_max_turns=3\nper_user_max_streams=5\n"))
    assert (overridden.per_user_max_turns, overridden.per_user_max_streams) == (7, 2)
    monkeypatch.delenv("AGENT_BASE_WEB_PER_USER_TURNS")
    monkeypatch.delenv("AGENT_BASE_WEB_PER_USER_STREAMS")

    for bad in ("0", "33", "-1", "3.5", '"abc"', "true"):
        with pytest.raises(ValueError, match="per_user_max_turns"):
            Config.load(write(f"[web]\nper_user_max_turns={bad}\n"))
    monkeypatch.setenv("AGENT_BASE_WEB_PER_USER_STREAMS", "0")
    with pytest.raises(ValueError, match="per_user_max_streams"):
        Config.load(write())
