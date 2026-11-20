"""Add chat ownership before serving requests; failures must abort startup.

NULL owner is an unassigned bootstrap conversation, NEVER public visibility.
NULL domain means the default domain; C3 read paths must not depend on NULL.
SYSTEM (identity.SYSTEM) is for internal callers only, never HTTP input;
this migration operates on all rows without user filtering.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

from .repositories import installation_lock

log = logging.getLogger(__name__)


def default_domain(conn):
    return conn.execute(
        "SELECT id FROM domains WHERE code='default' OR status='active' "
        "ORDER BY (code='default') DESC, created, id LIMIT 1").fetchone()


def migrate_chat(store, control):
    """Return per-update counts, preserving existing assignments and backup."""
    with installation_lock(store.root / "chat-migration.lock", 30):
        with store.connection() as conn:
            missing = [(table, column) for table, columns in (
                ("conversations", ("owner_user_id", "domain_id")),
                ("repo_installations", ("domain_id",)))
                for column in columns
                if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}]
            backup = store.db.with_name("runs.sqlite.pre-chat-multiuser")
            if missing and not backup.exists():
                # Online backup includes committed WAL data.
                try:
                    with sqlite3.connect(backup) as destination:
                        conn.backup(destination)
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            # One transaction for backfill and audit: a failed audit cannot leave
            # committed backfill that would silently omit the audit on retry.
            conn.execute("ATTACH DATABASE ? AS control", (str(control.db),))
            conn.execute("BEGIN IMMEDIATE")
            admin = conn.execute(
                "SELECT id FROM control.users WHERE username='admin' AND status='active'").fetchone()
            domain = conn.execute(
                "SELECT id FROM control.domains WHERE code='default' OR status='active' "
                "ORDER BY (code='default') DESC, created, id LIMIT 1").fetchone()
            admin_id, domain_id = (admin[0] if admin else None), (domain[0] if domain else None)
            if not admin:
                log.warning("chat migration: no active bootstrap admin; owner backfill skipped")
            if not domain:
                log.warning("chat migration: no default/active domain; domain backfill skipped")
            for table, column in missing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            for name, column in (("owner", "owner_user_id"), ("domain", "domain_id")):
                conn.execute(f"CREATE INDEX IF NOT EXISTS conversations_{name} "
                             f"ON conversations({column}, archived, updated)")
            counts = {}
            for key, table, column, value in (
                ("conversations_owner", "conversations", "owner_user_id", admin_id),
                ("conversations_domain", "conversations", "domain_id", domain_id),
                ("repo_installations_domain", "repo_installations", "domain_id", domain_id)):
                counts[key] = (conn.execute(f"UPDATE {table} SET {column}=? WHERE {column} IS NULL",
                                           (value,)).rowcount if value is not None else 0)
            if sum(counts.values()):
                conn.execute(
                    "INSERT INTO control.audit_log(id,domain_id,user_id,trace_id,level,action,detail,at) "
                    "VALUES(?,?,?,NULL,'chat','chat.migrate',?,?)",
                    (uuid.uuid4().hex, domain_id, None,
                     json.dumps({**counts, "bootstrap_admin_id": admin_id, "default_domain_id": domain_id}),
                     time.time()))
            return counts
