"""Migration tests use actual SQLite databases, including legacy WAL content."""
import sqlite3

import pytest

from agent_base.chat_migration import migrate_chat
from agent_base.governance.service import Governance
from agent_base.store import Store
from agent_base.web import create_app


def legacy(config):
    store = Store(config.data)
    with store.connection() as conn:
        conn.execute("INSERT INTO conversations(id,title,target,spec,created,updated) VALUES('old','old','admin','{}',1,1)")
        conn.execute("INSERT INTO repo_installations(id,repo_id,origin,commit_sha,root,manifest,status,created) "
                     "VALUES('repo','r','local','sha',?,'{}','ready',1)", (str(config.data / "legacy-repo"),))
    return store


def test_schema_backfill_backup_and_idempotency(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-password")
    store = legacy(config)
    app = create_app(config, background=False)
    control = app.state.governance.store
    admin = control.get_user_by_username("admin")
    with control.connection() as conn:
        domain = conn.execute("SELECT id FROM domains WHERE code='default'").fetchone()[0]
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM conversations").fetchone()
        assert (row["owner_user_id"], row["domain_id"]) == (admin["id"], domain)
        assert conn.execute("SELECT domain_id FROM repo_installations").fetchone()[0] == domain
        for name, column in (("owner", "owner_user_id"), ("domain", "domain_id")):
            assert [r["name"] for r in conn.execute(f"PRAGMA index_info(conversations_{name})")] == [column, "archived", "updated"]
        conn.execute("UPDATE conversations SET owner_user_id='assigned',domain_id='other'")
    backup = config.data / "runs.sqlite.pre-chat-multiuser"
    original = backup.read_bytes()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT id FROM conversations").fetchone()[0] == "old"
        assert "owner_user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert not sum(migrate_chat(store, control).values())
    create_app(config, background=False)
    assert backup.read_bytes() == original
    audits = [a for a in control.list_audit() if a["action"] == "chat.migrate"]
    assert len(audits) == 1
    assert audits[0]["level"] == "chat"
    assert audits[0]["detail"] == dict(conversations_owner=1, conversations_domain=1,
        repo_installations_domain=1, bootstrap_admin_id=admin["id"], default_domain_id=domain)
    with store.connection() as conn:
        assert conn.execute("SELECT owner_user_id,domain_id FROM conversations").fetchone()[:] == ("assigned", "other")


def test_missing_admin_and_domain_warn_and_later_backfill(config, caplog):
    store = legacy(config)
    gov = Governance(config)
    with gov.store.connection() as conn:
        conn.execute("UPDATE domains SET status='disabled',code='disabled'")
    counts = migrate_chat(store, gov.store)
    assert not sum(counts.values())
    assert "owner backfill skipped" in caplog.text
    assert "domain backfill skipped" in caplog.text
    with gov.store.connection() as conn:
        conn.execute("UPDATE domains SET status='active'")
    counts = migrate_chat(store, gov.store)
    assert counts == dict(conversations_owner=0, conversations_domain=1, repo_installations_domain=1)


def test_unreadable_control_aborts(config):
    store = legacy(config)
    gov = Governance(config)
    with gov.store.connection() as conn:
        conn.execute("ALTER TABLE users RENAME TO unavailable_users")
    with pytest.raises(sqlite3.OperationalError):
        migrate_chat(store, gov.store)
    with store.connection() as conn:
        assert "owner_user_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}


def test_startup_propagates_migration_failure(config, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("disk full or lock timeout")
    monkeypatch.setattr("agent_base.chat_migration.migrate_chat", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        create_app(config, background=False)


def test_audit_failure_rolls_back_backfill(config, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-password")
    store = legacy(config)
    gov = Governance(config)
    gov.bootstrap()
    with gov.store.connection() as conn:
        conn.execute("CREATE TRIGGER reject_migration BEFORE INSERT ON audit_log "
                     "WHEN NEW.action='chat.migrate' BEGIN SELECT RAISE(ABORT,'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="audit failed"):
        migrate_chat(store, gov.store)
    with gov.store.connection() as conn:
        conn.execute("DROP TRIGGER reject_migration")
    assert sum(migrate_chat(store, gov.store).values()) == 3


def test_install_http_writes_default_and_page_domain(config, resource_repos, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_base.repositories import Repositories
    monkeypatch.setenv("AGENT_BASE_ADMIN_PASSWORD", "fixture-password")
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "fixture-token")
    monkeypatch.setattr(Repositories, "ensure_service", lambda *args: None)
    app = create_app(config, background=False)
    gov = app.state.governance
    with gov.store.connection() as conn:
        default = conn.execute("SELECT id FROM domains WHERE code='default'").fetchone()[0]
    # A second active domain exercises page selection without changing repos auth.
    with gov.store.connection() as conn:
        conn.execute("INSERT INTO domains(id,code,name,status,created,updated) "
                     "VALUES('second','second','Second','active',2,2)")
    with TestClient(app) as client:
        first = client.post("/api/repos/install", headers={"Authorization": "Bearer fixture-token"},
                            json={"origin": str(resource_repos["finance"])})
        assert first.status_code == 201, first.text
        assert first.json()["domain_id"] == default
        assert client.post("/api/auth/login", json={"username": "admin", "password": "fixture-password"}).status_code == 200
        second = client.post("/api/repos/install", headers={"x-domain-id": "second"},
                             json={"origin": str(resource_repos["inventory"])})
        assert second.status_code == 201, second.text
        assert second.json()["domain_id"] == "second"
