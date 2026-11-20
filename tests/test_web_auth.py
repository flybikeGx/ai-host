"""Browser account login, server-side logout and workspace privilege boundary."""
import time

import pytest
from fastapi.testclient import TestClient

from agent_base.web import create_app
from agent_base.web_auth import COOKIE


@pytest.fixture
def signed_app(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-browser-password")
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "fixture-legacy-token")
    app = create_app(config, background=False)
    with TestClient(app) as client:
        yield client, app


def login(client, username="admin", password="fixture-browser-password"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_browser_login_restore_and_legacy_api(signed_app):
    client, app = signed_app
    assert client.get("/api/auth/session").json()["login_required"]
    assert client.get("/api/dashboard").status_code == 401
    assert login(client, password="wrong").status_code == 401
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=1200" in cookie
    assert "token" not in response.json()
    state = client.get("/api/auth/session").json()
    assert state["authenticated"] and state["can_manage_workspace"]
    assert state["user"]["username"] == "admin"
    assert not {"password_hash", "api_key_hash"} & state["user"].keys()
    assert client.get("/api/v1/me").status_code == 200
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/knowledge/bindings").status_code == 200
    # A separate browser/request can restore the session using its cookie.
    with TestClient(app) as restored:
        restored.cookies.set(COOKIE, client.cookies.get(COOKIE))
        assert restored.get("/api/auth/session").json()["authenticated"]


def test_logout_revokes_copied_cookie(signed_app):
    client, _ = signed_app
    login(client)
    old = client.cookies.get(COOKIE)
    assert client.post("/api/auth/logout").status_code == 200
    assert not client.get("/api/auth/session").json()["authenticated"]
    assert client.get("/api/dashboard", headers={"Cookie": f"{COOKIE}={old}"}).status_code == 401


def test_expired_or_disabled_session_cannot_access_workspace(signed_app):
    client, app = signed_app
    login(client)
    with app.state.governance.store.connection() as conn:
        conn.execute("UPDATE browser_sessions SET expires=?", (time.time() - 1,))
    assert not client.get("/api/auth/session").json()["authenticated"]
    assert client.get("/api/dashboard").status_code == 401
    login(client)
    user = app.state.governance.store.get_user_by_username("admin")
    app.state.governance.store.update_user(user["id"], status="disabled")
    assert client.get("/api/dashboard").status_code == 401
    assert not client.get("/api/auth/session").json()["authenticated"]


def test_members_and_partial_admins_do_not_gain_global_workspace(signed_app):
    client, app = signed_app
    gov = app.state.governance
    member = gov.create_user("member", password="fixture-member-password")
    assert login(client, "member", "fixture-member-password").status_code == 200
    state = client.get("/api/auth/session").json()
    assert state["authenticated"] and not state["can_manage_workspace"]
    assert client.get("/api/v1/me").json()["id"] == member["id"]
    for path in ("/api/dashboard", "/api/chat/targets", "/api/services", "/api/knowledge/namespaces"):
        assert client.get(path).status_code == 403
    login(client)
    admin = gov.store.get_user_by_username("admin")
    gov.grant_permission(admin["id"], "file.delete", effect="deny")
    assert not client.get("/api/auth/session").json()["can_manage_workspace"]
    assert client.get("/api/dashboard").status_code == 403


def test_anonymous_legacy_mode_and_explicit_management_token(config, monkeypatch):
    monkeypatch.delenv("AGENT_BASE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_BASE_WEB_TOKEN", raising=False)
    app = create_app(config, background=False)
    with TestClient(app) as client:
        assert not client.get("/api/auth/session").json()["login_required"]
        assert client.get("/api/dashboard").status_code == 200
        app.state.governance.bootstrap(admin_password="fixture-password")
        assert client.get("/api/dashboard").status_code == 401
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "fixture-legacy-token")
    with TestClient(create_app(config, background=False)) as client:
        assert client.get("/api/dashboard", headers={"Authorization": "Bearer fixture-legacy-token"}).status_code == 200


def test_login_csrf_and_sso_capabilities(signed_app):
    client, _ = signed_app
    denied = client.post("/api/auth/login", json={"username": "admin", "password": "fixture-browser-password"},
                         headers={"Origin": "https://other.invalid"})
    assert denied.status_code == 403
    state = client.get("/api/auth/session").json()
    assert state["sso"] == {"identity_verification_configured": False, "browser_login_available": False}
    assert client.get("/login").status_code == 200


def test_successful_login_after_lock_expiry_creates_valid_session(signed_app):
    client, app = signed_app
    admin = app.state.governance.store.get_user_by_username("admin")
    app.state.governance.store.update_user(admin["id"], status="locked", locked_until=time.time() - 1)
    assert login(client).status_code == 200
    assert client.get("/api/auth/session").json()["authenticated"]
