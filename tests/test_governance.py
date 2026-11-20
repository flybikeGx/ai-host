"""治理层：身份 / RBAC / 分权分域 / 审计 的非侵入实现验证。

覆盖密码往返、令牌防篡改、deny 优先、域隔离、以及 /api/v1 的登录与权限门控。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_base.governance import auth
from agent_base.governance.service import Governance


def test_password_round_trip():
    encoded = auth.hash_password("s3cret")
    assert encoded.startswith("pbkdf2_sha256$")
    assert auth.verify_password("s3cret", encoded)
    assert not auth.verify_password("wrong", encoded)


def test_bootstrap_and_login(config):
    gov = Governance(config)
    assert gov.bootstrap(admin_password="fixture-admin-pw")["admin_created"] is True
    # 二次引导幂等，不重建 admin
    assert gov.bootstrap(admin_password="fixture-admin-pw")["admin_created"] is False

    session = gov.login("admin", "fixture-admin-pw")
    assert session["token"]
    assert session["user"]["username"] == "admin"
    assert "password_hash" not in session["user"]
    payload = gov.decode(session["token"])
    assert payload["sub"] == session["user"]["id"]

    with pytest.raises(ValueError):
        gov.login("admin", "wrong")
    with pytest.raises(ValueError):
        gov.login("nobody", "whatever")


def test_token_tampering_rejected(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    token = gov.login("admin", "pw")["token"]
    last = token[-1]
    tampered = token[:-1] + ("A" if last != "A" else "B")
    with pytest.raises(ValueError):
        auth.decode_token(tampered, gov._secret)


def test_list_roles_includes_permissions(config):
    gov = Governance(config)
    roles = gov.list_roles()
    assert len(roles) == 6  # 内置角色已种子化
    platform = next(r for r in roles if r["name"] == "platform_admin")
    assert "user.manage" in platform["permissions"]


def test_rbac_deny_wins_over_allow(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    domain = gov.list_domains()[0]
    user = gov.create_user("alice", password="pw", home_domain_id=domain["id"])

    gov.grant_permission(user["id"], "agent.run", effect="allow", domain_id=domain["id"])
    assert "agent.run" in gov.effective_permissions(user["id"], domain["id"])

    gov.grant_permission(user["id"], "agent.run", effect="deny", domain_id=domain["id"])
    assert "agent.run" not in gov.effective_permissions(user["id"], domain["id"])


def test_domain_scoped_role_isolation(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    d1 = gov.create_domain("d1", "域一")
    d2 = gov.create_domain("d2", "域二")
    user = gov.create_user("bob", password="pw")

    viewer = gov.store.get_role_by_name("viewer")
    gov.grant_role(user["id"], viewer["id"], domain_id=d1["id"])

    assert "service.read" in gov.effective_permissions(user["id"], d1["id"])
    assert "service.read" not in gov.effective_permissions(user["id"], d2["id"])
    assert "service.read" not in gov.effective_permissions(user["id"], None)


def test_service_account_api_key_login(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    domain = gov.list_domains()[0]
    created = gov.create_user("bot", user_type="service", home_domain_id=domain["id"])
    assert "api_key" in created

    session = gov.login_with_api_key(created["api_key"])
    assert session["user"]["username"] == "bot"
    with pytest.raises(ValueError):
        gov.login_with_api_key("not-a-real-key")


def test_http_login_and_permission_gated_crud(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        # 未登录：401
        assert client.get("/api/v1/users").status_code == 401

        login = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "fixture-admin-pw"})
        assert login.status_code == 200
        token = login.json()["token"]
        admin_h = {"Authorization": "Bearer " + token}

        assert client.get("/api/v1/me", headers=admin_h).status_code == 200
        # 平台管理员拥有 user.manage
        assert client.get("/api/v1/users", headers=admin_h).status_code == 200
        assert client.get("/api/v1/domains", headers=admin_h).status_code == 200

        created = client.post("/api/v1/users", headers=admin_h,
                              json={"username": "alice", "password": "alice-pw"})
        assert created.status_code == 201

        # 普通成员无 user.manage → 403
        alice_login = client.post("/api/v1/auth/login",
                                  json={"username": "alice", "password": "alice-pw"})
        alice_h = {"Authorization": "Bearer " + alice_login.json()["token"]}
        assert client.get("/api/v1/users", headers=alice_h).status_code == 403

        # 审计记录了引导与用户创建
        audit = client.get("/api/v1/audit", headers=admin_h)
        assert audit.status_code == 200
        actions = {item["action"] for item in audit.json()}
        assert {"auth.bootstrap", "user.create"} <= actions


# ---- 账号生命周期 ----

def test_account_lockout_after_repeated_failures(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    for _ in range(5):
        with pytest.raises(ValueError):
            gov.login("admin", "bad")
    assert gov.store.get_user_by_username("admin")["status"] == "locked"
    with pytest.raises(ValueError, match="锁定"):
        gov.login("admin", "pw")


def test_disabled_account_rejected(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    user = gov.create_user("carol", password="pw")
    gov.update_user(user["id"], status="disabled")
    with pytest.raises(ValueError, match="停用"):
        gov.login("carol", "pw")


def test_expired_token_rejected():
    token = auth.issue_token({"sub": "x", "iat": 0}, "secret", ttl=-1)
    with pytest.raises(ValueError, match="过期"):
        auth.decode_token(token, "secret")


# ---- 角色 / 权限管理 ----

def test_create_role_with_permissions(config):
    gov = Governance(config)
    role = gov.create_role("ops", description="运维", permissions=["service.read", "agent.run"])
    assert role["is_builtin"] == 0
    assert set(role["permissions"]) == {"service.read", "agent.run"}


def test_builtin_role_permissions_immutable(config):
    gov = Governance(config)
    viewer = gov.store.get_role_by_name("viewer")
    with pytest.raises(ValueError, match="内置角色"):
        gov.update_role(viewer["id"], permissions=["audit.read"])


def test_unknown_permission_rejected(config):
    gov = Governance(config)
    with pytest.raises(ValueError, match="未知权限"):
        gov.create_role("bad", permissions=["not.a.perm"])
    user = gov.create_user("dave", password="pw")
    with pytest.raises(ValueError, match="未知权限"):
        gov.grant_permission(user["id"], "not.a.perm")


# ---- 域 / 用户校验 ----

def test_duplicate_username_and_domain_code_rejected(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    gov.create_domain("team", "团队")
    with pytest.raises(ValueError, match="域码已存在"):
        gov.create_domain("team", "重复")
    with pytest.raises(ValueError, match="已存在"):
        gov.create_user("admin", password="x")


def test_invalid_update_fields_rejected(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    user = gov.create_user("eve", password="pw")
    with pytest.raises(ValueError, match="无效用户字段"):
        gov.update_user(user["id"], is_admin=True)
    domain = gov.list_domains()[0]
    with pytest.raises(ValueError, match="无效域字段"):
        gov.update_domain(domain["id"], bogus="x")


# ---- 授权 ----

def test_grant_and_revoke_role(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    user = gov.create_user("frank", password="pw")
    domain = gov.list_domains()[0]
    viewer = gov.store.get_role_by_name("viewer")
    grant = gov.grant_role(user["id"], viewer["id"], domain_id=domain["id"])
    assert "service.read" in gov.effective_permissions(user["id"], domain["id"])
    gov.revoke_role(grant["id"])
    assert "service.read" not in gov.effective_permissions(user["id"], domain["id"])


def test_grant_permission_invalid_effect(config):
    gov = Governance(config)
    user = gov.create_user("grace", password="pw")
    with pytest.raises(ValueError, match="effect"):
        gov.grant_permission(user["id"], "agent.run", effect="maybe")


# ---- 成员与分组 ----

def test_membership_add_remove(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    user = gov.create_user("henry", password="pw")
    d1 = gov.create_domain("m1", "成员域")
    gov.add_membership(user["id"], d1["id"])
    assert {m["domain_id"] for m in gov.principal(user["id"])["domains"]} == {d1["id"]}
    gov.remove_membership(user["id"], d1["id"])
    assert gov.principal(user["id"])["domains"] == []


def test_group_kind_validation(config):
    gov = Governance(config)
    domain = gov.list_domains()[0]
    with pytest.raises(ValueError, match="kind"):
        gov.create_group(domain["id"], "bogus", "坏分组")
    group = gov.create_group(domain["id"], "service", "执行组")
    assert gov.list_groups(domain["id"])[0]["name"] == "执行组"


# ---- 审计过滤 ----

def test_audit_filtering(config):
    gov = Governance(config)
    gov.bootstrap(admin_password="pw")
    d1 = gov.create_domain("a1", "域A")
    d2 = gov.create_domain("a2", "域B")

    actions = [a["action"] for a in gov.list_audit(limit=100)]
    assert actions.count("domain.create") == 2
    assert "auth.bootstrap" in actions

    assert [a["action"] for a in gov.list_audit(domain_id=d1["id"])] == ["domain.create"]
    assert len(gov.list_audit(limit=1)) == 1


# ---- HTTP 端点 ----

def test_http_domain_role_and_grant_endpoints(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        token = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
        h = {"Authorization": "Bearer " + token}

        domain = client.post("/api/v1/domains", headers=h, json={"code": "web", "name": "Web域"})
        assert domain.status_code == 201
        domain_id = domain.json()["id"]

        role = client.post("/api/v1/roles", headers=h,
                           json={"name": "ops", "permissions": ["service.read"]})
        assert role.status_code == 201
        role_id = role.json()["id"]

        user = client.post("/api/v1/users", headers=h, json={"username": "webuser", "password": "webpw"})
        assert user.status_code == 201
        user_id = user.json()["id"]

        assert client.post("/api/v1/memberships", headers=h,
                           json={"user_id": user_id, "domain_id": domain_id}).status_code == 201
        grant = client.post("/api/v1/grants/roles", headers=h,
                            json={"user_id": user_id, "role_id": role_id, "domain_id": domain_id})
        assert grant.status_code == 201
        assert client.post("/api/v1/groups", headers=h,
                           json={"domain_id": domain_id, "kind": "service", "name": "grp"}).status_code == 201

        # me 携带有效权限
        me = client.get("/api/v1/me", headers=h).json()
        assert "effective_permissions" in me


def test_http_login_failure_and_api_key(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        assert client.post("/api/v1/auth/login",
                           json={"username": "admin", "password": "wrong"}).status_code == 401

        token = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
        h = {"Authorization": "Bearer " + token}
        created = client.post("/api/v1/users", headers=h, json={"username": "bot", "user_type": "service"})
        assert created.status_code == 201
        key = created.json()["api_key"]

        session = client.post("/api/v1/auth/api-key", json={"key": key})
        assert session.status_code == 200 and session.json()["user"]["username"] == "bot"
        assert client.post("/api/v1/auth/api-key", json={"key": "bogus"}).status_code == 401


def test_http_domain_scoped_permission_header(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-admin-pw")
    from agent_base.web import create_app
    with TestClient(create_app(config, background=False)) as client:
        token = client.post("/api/v1/auth/login",
                            json={"username": "admin", "password": "fixture-admin-pw"}).json()["token"]
        h = {"Authorization": "Bearer " + token}

        domain = client.post("/api/v1/domains", headers=h, json={"code": "scoped", "name": "Scoped"}).json()
        roles = client.get("/api/v1/roles", headers=h).json()
        domain_admin_id = next(r["id"] for r in roles if r["name"] == "domain_admin")
        da = client.post("/api/v1/users", headers=h, json={"username": "da", "password": "dapw"}).json()

        client.post("/api/v1/grants/roles", headers=h,
                    json={"user_id": da["id"], "role_id": domain_admin_id, "domain_id": domain["id"]})

        da_h = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login", json={"username": "da", "password": "dapw"}).json()["token"]}
        # 无域上下文 → 无全局 user.manage → 403
        assert client.get("/api/v1/users", headers=da_h).status_code == 403
        # 携带 x-domain-id 命中域内授权 → 200
        assert client.get("/api/v1/users", headers={**da_h, "x-domain-id": domain["id"]}).status_code == 200


def test_refresh_and_per_request_account_revalidation(config, monkeypatch):
    import time
    from agent_base.web import create_app
    monkeypatch.setenv('AGENT_BASE_ADMIN_PASSWORD', 'fixture-password')
    monkeypatch.setenv('AGENT_BASE_WEB_AUTH', 'jwt')
    app = create_app(config, background=False)
    gov = app.state.governance
    with TestClient(app) as client:
        old = gov.login('admin', 'fixture-password')
        header = {'Authorization': 'Bearer ' + old['token']}
        renewed = client.post('/api/v1/auth/refresh', headers=header)
        assert renewed.status_code == 200
        assert renewed.json()['expires_in'] == 1200
        new = gov.decode(renewed.json()['token'])
        assert new['jti'] != gov.decode(old['token'])['jti']
        assert new['iat'] <= time.time() < new['exp']
        for token in (old['token'], renewed.json()['token']):
            assert client.get('/api/v1/me', headers={'Authorization': 'Bearer ' + token}).status_code == 200
        expired = auth.issue_token({'sub': old['user']['id']}, gov._secret, ttl=-1)
        for path in ('/api/v1/auth/refresh', '/api/v1/me'):
            request = client.post if path.endswith('refresh') else client.get
            for token, code in ((None, 'token_missing'), ('invalid', 'token_invalid'), (expired, 'token_expired')):
                response = request(path, headers={'Authorization': 'Bearer ' + token} if token else {})
                assert response.status_code == 401 and response.json()['code'] == code
        for status in ('locked', 'disabled'):
            gov.store.update_user(old['user']['id'], status=status)
            for path in ('/api/v1/me', '/api/v1/users'):
                assert client.get(path, headers=header).json()['code'] == 'user_disabled'
            assert client.post('/api/v1/auth/refresh', headers=header).status_code == 401
        gov.store.update_user(old['user']['id'], status='active')
        client.post('/api/auth/login', json={'username': 'admin', 'password': 'fixture-password'})
        assert client.post('/api/v1/auth/refresh').json()['code'] == 'token_missing'


@pytest.mark.parametrize('token', ['☃.sig', 'body.☃', '.', 'invalid', ''])
def test_malformed_tokens_are_value_errors(token):
    with pytest.raises(ValueError):
        auth.decode_token(token, 'fixture-secret')
