from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted", "prepared"}
ACTIVE = {"preparing", "running", "verifying"}


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = root / "runs.sqlite"
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, spec TEXT NOT NULL, root TEXT NOT NULL,
                    pid INTEGER, birth REAL, cancel_requested INTEGER DEFAULT 0,
                    error TEXT, result TEXT, session_id TEXT, prepared INTEGER DEFAULT 0,
                    executor_started INTEGER DEFAULT 0, process_pid INTEGER, process_birth REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    name TEXT PRIMARY KEY, native_id TEXT
                );
                CREATE TABLE IF NOT EXISTS works (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, definition TEXT NOT NULL,
                    next_due REAL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS triggers (
                    work_id TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL,
                    source TEXT NOT NULL, run_id TEXT, disposition TEXT NOT NULL,
                    PRIMARY KEY(work_id,key)
                );
                CREATE TABLE IF NOT EXISTS repo_installations (
                    id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, origin TEXT NOT NULL,
                    commit_sha TEXT NOT NULL, root TEXT NOT NULL, manifest TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, enabled INTEGER DEFAULT 1,
                    pid INTEGER, birth REAL, endpoint TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, target TEXT NOT NULL,
                    spec TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    native_id TEXT, archived INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, client_key TEXT NOT NULL,
                    user_text TEXT NOT NULL, assistant_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    error TEXT, pid INTEGER, birth REAL, executor_started INTEGER DEFAULT 0,
                    cancel_requested INTEGER DEFAULT 0, run_id TEXT, root TEXT NOT NULL,
                    child_pid INTEGER, child_birth REAL,
                    UNIQUE(conversation_id,client_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_one_active ON conversation_turns(conversation_id)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS conversation_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL, at REAL NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_events_cursor ON conversation_events(conversation_id,seq);
            """)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            for name, kind in (("executor_started", "INTEGER DEFAULT 0"), ("process_pid", "INTEGER"), ("process_birth", "REAL"),
                               ("dispatched", "INTEGER DEFAULT 1"), ("work_id", "TEXT"), ("workspace", "TEXT"),
                               ("started_at", "REAL"), ("finished_at", "REAL"), ("deadline", "REAL"),
                               ("heartbeat", "REAL"), ("session_status", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
            conn.execute("CREATE INDEX IF NOT EXISTS runs_queue ON runs(status,dispatched,created)")
            conn.execute("CREATE INDEX IF NOT EXISTS runs_work ON runs(work_id,status)")
            repo_columns = {r[1] for r in conn.execute("PRAGMA table_info(repo_installations)")}
            for name, kind in (("restart_attempts", "INTEGER DEFAULT 0"), ("next_retry", "REAL DEFAULT 0")):
                if name not in repo_columns:
                    conn.execute(f"ALTER TABLE repo_installations ADD COLUMN {name} {kind}")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert(self, conn, spec: dict, *, dispatched=1, work_id=None) -> str:
        run_id = uuid.uuid4().hex
        root = self.root / "runs" / run_id
        root.mkdir(parents=True, mode=0o700)
        now = time.time()
        conn.execute("INSERT INTO runs(id,status,created,updated,spec,root,dispatched,work_id) VALUES(?,?,?,?,?,?,?,?)",
                     (run_id, "queued", now, now, json.dumps(spec), str(root), dispatched, work_id))
        conn.execute("INSERT INTO events(run_id,at,type,data) VALUES(?,?,?,?)", (run_id, now, "run.queued", "{}"))
        return run_id

    def create(self, spec: dict) -> dict:
        with self.connection() as conn:
            run_id = self.insert(conn, spec)
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"任务不存在：{run_id}")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def list(self, limit: int = 30) -> list[dict]:
        with self.connection() as conn:
            ids = conn.execute("SELECT id FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [self.get(row[0]) for row in ids]

    def update(self, run_id: str, **fields):
        allowed = {"status", "pid", "birth", "cancel_requested", "error", "result", "session_id", "prepared", "spec", "process_pid", "process_birth",
                   "workspace", "started_at", "finished_at", "deadline", "heartbeat", "session_status"}
        if not fields or set(fields) - allowed:
            raise ValueError("无效任务字段")
        for key in ("spec", "result"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated"] = time.time()
        if fields.get("status") in TERMINAL:
            fields["finished_at"] = time.time()
        with self.connection() as conn:
            conn.execute(f"UPDATE runs SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), run_id))

    def claim(self, run_id: str, cap: int) -> bool:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,cancel_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row[0] != "queued" or row[1]:
                return False
            # Stable FIFO across independently launched task processes.
            first = conn.execute("SELECT id FROM runs WHERE status='queued' AND cancel_requested=0 ORDER BY created,id LIMIT 1").fetchone()
            count = conn.execute("SELECT count(*) FROM runs WHERE status IN ('preparing','running','verifying')").fetchone()[0]
            if count >= cap or first[0] != run_id:
                return False
            conn.execute("UPDATE runs SET status='preparing',updated=? WHERE id=?", (time.time(), run_id))
            return True

    def event(self, run_id: str, event_type: str, data: dict):
        with self.connection() as conn:
            conn.execute("INSERT INTO events(run_id,at,type,data) VALUES(?,?,?,?)",
                         (run_id, time.time(), event_type, json.dumps(data, ensure_ascii=False)))

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        self.get(run_id)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, after)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def chat_id(self, name: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT native_id FROM chat_sessions WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def save_chat_id(self, name: str, native_id: str):
        with self.connection() as conn:
            conn.execute("INSERT INTO chat_sessions VALUES(?,?) ON CONFLICT(name) DO UPDATE SET native_id=excluded.native_id", (name, native_id))
