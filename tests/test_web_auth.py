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


def test_identity_bearer_cookie_equivalence_and_stale_claims(signed_app):
    client, app = signed_app
    from agent_base.identity import get_identity
    from fastapi import Depends

    @app.get('/api/identity-probe')
    def probe(identity=Depends(get_identity)):
        return {"id": identity.user_id, "username": identity.username,
                "type": identity.user_type, "domain": identity.current_domain,
                "permissions": sorted(identity.permissions), "mode": identity.mode}

    login(client)
    session = app.state.governance.login('admin', 'fixture-browser-password')
    bearer = {"Authorization": "Bearer " + session['token']}
    cookie_identity = client.get('/api/identity-probe').json()
    assert client.get('/api/identity-probe', headers=bearer).json() == cookie_identity
    # Explicit invalid credentials must never fall back to a valid cookie.
    assert client.get('/api/identity-probe', headers={"Authorization": "Basic invalid"}).status_code == 401
    assert client.get('/api/identity-probe', headers={"Authorization": "Bearer invalid"}).status_code == 401
    gov = app.state.governance
    gov.store.update_user(session['user']['id'], status='disabled')
    for headers in ({}, bearer):
        response = client.get('/api/v1/me', headers=headers)
        assert response.status_code == 401
        assert response.json()['code'] == 'user_disabled'


def test_identity_bootstrap_loopback_and_jwt_mode(config, monkeypatch, caplog):
    from agent_base.identity import get_identity
    from fastapi import Depends
    monkeypatch.setenv('AGENT_BASE_ADMIN_PASSWORD', 'fixture-password')
    monkeypatch.delenv('AGENT_BASE_WEB_TOKEN', raising=False)
    app = create_app(config, background=False)

    @app.get('/api/identity-probe')
    def probe(identity=Depends(get_identity)):
        return {"id": identity.user_id, "type": identity.user_type,
                "mode": identity.mode, "domain": identity.current_domain}

    assert caplog.text.count('本机引导模式，未启用登录') == 1
    with TestClient(app, client=('127.0.0.1', 1234)) as local:
        result = local.get('/api/identity-probe').json()
        assert result['mode'] == 'bootstrap'
        assert result['id'] == app.state.governance.store.get_user_by_username('admin')['id']
        default = app.state.governance.store.get_domain_by_code('default')
        assert result['domain'] == default['id']
        assert local.get('/api/v1/me').status_code == 200
    with TestClient(app, client=('192.0.2.3', 1234)) as remote:
        assert remote.get('/api/identity-probe', headers={'Host': 'localhost', 'X-Forwarded-For': '127.0.0.1'}).status_code == 401
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN', 'fixture-shared')
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'jwt')
    with TestClient(create_app(config, background=False), client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/v1/me').json()['code'] == 'token_missing'
        assert client.get('/api/v1/me', headers={'Authorization': 'Bearer fixture-shared'}).json()['code'] == 'token_invalid'
        assert login(client, password='fixture-password').status_code == 200
        # A valid cookie cannot make the explicitly rejected shared token valid.
        assert client.get('/api/dashboard', headers={'Authorization': 'Bearer fixture-shared'}).status_code == 401
        assert client.get('/api/v1/me').status_code == 200


def test_invalid_auth_mode_fails_startup(config, monkeypatch):
    config.web = {'auth': 'unsupported'}
    with pytest.raises(ValueError, match='web.auth'):
        create_app(config, background=False)
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'jwt')
    assert create_app(config, background=False).state.identity_resolver.mode == 'jwt'
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'invalid')
    with pytest.raises(ValueError, match='web.auth'):
        create_app(config, background=False)


def test_domain_resolution_and_permissions(signed_app):
    from fastapi import Depends
    from agent_base.identity import get_identity
    client, app = signed_app
    gov = app.state.governance

    @app.get('/api/identity-probe')
    def probe(identity=Depends(get_identity)):
        return {'domain': identity.current_domain, 'can_read': identity.can('agent.read'),
                'admin': identity.platform_admin, 'domains': sorted(identity.domains)}

    first = gov.create_domain('first', 'First')
    second = gov.create_domain('second', 'Second')
    user = gov.create_user('domain-member', password='fixture-pass')
    gov.store.add_membership(user['id'], first['id'])
    viewer = gov.store.get_role_by_name('viewer')
    gov.grant_role(user['id'], viewer['id'], domain_id=first['id'])
    token = gov.login('domain-member', 'fixture-pass')['token']
    headers = {'Authorization': 'Bearer ' + token}
    result = client.get('/api/identity-probe', headers=headers).json()
    assert result['domain'] == first['id'] and result['can_read'] and not result['admin']
    me = client.get('/api/v1/me', headers=headers).json()
    assert me['roles'] and me['effective_permissions'] == []
    assert me['domains'][0]['permissions'] == sorted(gov.effective_permissions(user['id'], first['id']))
    assert me['domains'][0]['joined_at']
    for domain in ('first', 'missing', second['id']):
        denied = client.get('/api/identity-probe', headers={**headers, 'x-domain-id': domain})
        assert denied.status_code == 403 and denied.json()['code'] == 'domain_forbidden'
    gov.store.add_membership(user['id'], second['id'])
    assert client.get('/api/identity-probe', headers=headers).json()['code'] == 'domain_required'
    gov.store.update_user(user['id'], home_domain_id=second['id'])
    assert client.get('/api/identity-probe', headers=headers).json()['domain'] == second['id']
    gov.store.update_domain(second['id'], status='disabled')
    assert client.get('/api/identity-probe', headers=headers).json()['domain'] == first['id']
    # Control plane remains usable without membership, including global grants.
    third = gov.create_domain('third', 'Third')
    gov.grant_permission(user['id'], 'user.manage')
    assert client.get('/api/v1/users', headers={**headers, 'x-domain-id': third['id']}).status_code == 200
    assert client.get('/api/v1/me', headers={**headers, 'x-domain-id': 'missing'}).json()['code'] == 'domain_forbidden'
    gov.store.remove_membership(user['id'], first['id'])
    assert client.get('/api/identity-probe', headers=headers).json()['code'] == 'domain_forbidden'


def test_bootstrap_shared_token_anonymous_and_domain_fallback(config, monkeypatch):
    from agent_base.identity import get_identity
    from fastapi import Depends
    monkeypatch.delenv('AGENT_BASE_ADMIN_PASSWORD', raising=False)
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN', 'fixture-shared')
    app = create_app(config, background=False)

    @app.get('/api/identity-probe')
    def probe(identity=Depends(get_identity)):
        return {'id': identity.user_id, 'domain': identity.current_domain,
                'mode': identity.mode, 'bootstrap': identity.is_bootstrap}

    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer fixture-shared', 'x-domain-id': 'invalid'}
        result = client.get('/api/identity-probe', headers=headers).json()
        assert result['id'] is None and result['bootstrap'] and result['mode'] == 'token'
        assert result['domain'] == app.state.governance.store.get_domain_by_code('default')['id']
        assert client.post('/api/v1/auth/refresh', headers={'Authorization': 'Bearer fixture-shared'}).status_code == 401
        assert client.get('/api/v1/me', headers={'Authorization': 'Bearer fixture-shared'}).json()['platform_admin']


def test_service_identity_admin_grant_expiry_and_internal_system(signed_app):
    from dataclasses import FrozenInstanceError
    from fastapi import Depends
    from agent_base.identity import get_identity, system_identity
    client, app = signed_app
    gov = app.state.governance

    @app.get('/api/identity-probe')
    def probe(identity=Depends(get_identity)):
        return {'type': identity.user_type, 'admin': identity.platform_admin,
                'mode': identity.mode}

    default = gov.store.get_domain_by_code('default')
    bot = gov.create_user('fixture-bot', user_type='service')
    gov.store.add_membership(bot['id'], default['id'])
    role = gov.store.get_role_by_name('platform_admin')
    gov.store.grant_role(bot['id'], role['id'], expires_at=time.time() - 1)
    token = gov.login_with_api_key(bot['api_key'])['token']
    headers = {'Authorization': 'Bearer ' + token}
    result = client.get('/api/identity-probe', headers=headers).json()
    assert result == {'type': 'service', 'admin': False, 'mode': 'jwt'}
    assert client.post('/api/v1/auth/refresh', headers=headers).json()['expires_in'] == 1200
    gov.grant_role(bot['id'], role['id'], domain_id=default['id'])
    assert not client.get('/api/identity-probe', headers=headers).json()['admin']
    global_grant = gov.grant_role(bot['id'], role['id'])
    assert client.get('/api/identity-probe', headers=headers).json()['admin']
    gov.store.revoke_role(global_grant['id'])
    assert not client.get('/api/identity-probe', headers=headers).json()['admin']
    system = system_identity(gov)
    assert system.mode == 'system' and system.can('agent.run')
    assert default['id'] in system.domains
    with pytest.raises(FrozenInstanceError):
        system.mode = 'jwt'


def test_identity_dependency_parses_and_evaluates_once(signed_app, monkeypatch):
    client, app = signed_app
    gov = app.state.governance
    header = {'Authorization': 'Bearer ' + gov.login('admin', 'fixture-browser-password')['token']}
    counts = {'decode': 0, 'permissions': 0}
    decode, permissions = gov.decode, gov.effective_permissions

    def counted_decode(token):
        counts['decode'] += 1
        return decode(token)

    def counted_permissions(*args):
        counts['permissions'] += 1
        return permissions(*args)

    monkeypatch.setattr(gov, 'decode', counted_decode)
    monkeypatch.setattr(gov, 'effective_permissions', counted_permissions)
    assert client.post('/api/v1/domains', json={'code': 'fixture-once', 'name': 'Once'}, headers=header).status_code == 201
    assert counts == {'decode': 1, 'permissions': 1}


def test_auth_mode_loaded_from_toml_and_environment(config, monkeypatch):
    from agent_base.config import Config
    with config.path.open('a') as stream:
        stream.write('\n[web]\nauth="jwt"\n')
    assert Config.load(config.path).web_auth_mode() == 'jwt'
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'hybrid')
    assert Config.load(config.path).web_auth_mode() == 'hybrid'
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'unsupported')
    with pytest.raises(ValueError, match='web.auth'):
        Config.load(config.path)
