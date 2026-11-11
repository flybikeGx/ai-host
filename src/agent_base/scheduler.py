"""Durable, single-host queue shared by cron, HTTP, CLI and admin MCP."""
from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from croniter import croniter

from .processes import Cancelled, DeadlineExceeded
from .service import Service


def next_slot(expression: str, timezone: str, after: float) -> float:
    # Five-field cron; no implicit seconds/year fields. Limit pathological searches.
    return croniter(expression, datetime.fromtimestamp(after, ZoneInfo(timezone)),
                    max_years_between_matches=5).get_next(datetime).timestamp()


def copy_inputs(source: Path, target: Path, deadline: float, cancelled):
    """Explicit input tree only; never dereference symlinks or copy credentials."""
    ignored = {".git", ".venv", "node_modules", "__pycache__", ".agent-base", ".aws", ".ssh", ".codex", ".agents"}
    total = 0
    count = 0
    if target.exists() or target.is_symlink():
        raise ValueError("仓库已含 input/，不能覆盖；请为外部工作数据保留 input/ 目录名")
    target.mkdir()

    def visit(directory: Path, destination: Path):
        nonlocal total, count
        for entry in directory.iterdir():
            if cancelled():
                raise Cancelled("复制工作区时取消")
            if time.time() >= deadline:
                raise DeadlineExceeded("复制工作区超时")
            if entry.name in ignored or entry.name == ".env" or entry.name.startswith(".env."):
                continue
            if entry.is_symlink():
                raise ValueError(f"输入工作区含符号链接，拒绝复制：{entry}")
            dest = destination / entry.name
            count += 1
            if count > 10000:
                raise ValueError("输入工作区超过 10000 个条目")
            if entry.is_dir():
                dest.mkdir()
                visit(entry, dest)
            elif entry.is_file():
                total += entry.stat().st_size
                if total > 256 * 1024 * 1024:
                    raise ValueError("输入工作区超过 256 MiB")
                shutil.copyfile(entry, dest)
            else:
                raise ValueError(f"输入工作区含非普通文件：{entry}")

    visit(source, target)


class Scheduler:
    def __init__(self, config):
        self.service = Service(config)
        self.store = self.service.store
        self.stop_event = threading.Event()
        self.last_service_check = 0
        self.last_tick_at = None
        self.last_error_at = None

    def register(self, name: str, repo: str, *, task=None, ref=None, model=None, effort=None,
                 workspace_source=None, timeout_seconds=1800, cron=None, timezone="Asia/Shanghai",
                 overlap="skip", now=None, run_at=None, schedule=None, installation_id=None) -> dict:
        now = time.time() if now is None else now
        if run_at is not None and (cron or isinstance(run_at, bool) or not isinstance(run_at, (int, float))
                                   or not math.isfinite(run_at) or run_at <= now):
            raise ValueError("一次性执行时间必须在未来，且不能同时设置 cron")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("工作名须为 1–64 位字母、数字、下划线或连字符")
        if overlap not in {"skip", "allow"}:
            raise ValueError("overlap 必须是 skip 或 allow")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 86400:
            raise ValueError("timeout_seconds 必须在 (0,86400] 内")
        if cron and (len(cron.split()) != 5 or not croniter.is_valid(cron)):
            raise ValueError("cron 须为有效的五段表达式")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"无效时区：{timezone}") from error
        if workspace_source:
            source = Path(workspace_source).expanduser().resolve()
            if not source.is_dir() or source == source.parent:
                raise ValueError("workspace_source 必须是专用的现有输入目录")
            if source == self.store.root or source in self.store.root.parents or self.store.root in source.parents:
                raise ValueError("输入目录不可与基座状态目录重叠")
            workspace_source = str(source)
        spec = self.service.specification(repo, task, ref=ref, model=model, effort=effort)
        if installation_id:
            from .repositories import Repositories
            installed = Repositories(self.service.config).get(installation_id)
            if installed["origin"] != spec["repo"] or installed["commit_sha"] != ref or not installed["enabled"] or installed["status"] != "ready":
                raise ValueError("定时计划的安装版本与仓库不匹配或已停用")
            spec["installation_id"] = installation_id
        spec["runtime"]["timeout_seconds"] = timeout_seconds
        spec.update(temporary_workspace=True, workspace_source=workspace_source)
        definition = {"name": name, "spec": spec, "cron": cron, "timezone": timezone, "overlap": overlap}
        if run_at is not None:
            definition["run_at"] = run_at
        if schedule is not None:
            definition["schedule"] = schedule
        due = run_at if run_at is not None else next_slot(cron, timezone, now) if cron else None
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM works WHERE id=?", (name,)).fetchone():
                raise ValueError("工作已存在；首版使用新名称注册新版本，避免悄悄修改已排队任务")
            conn.execute("INSERT INTO works VALUES(?,?,?,?,1)", (name, now, json.dumps(definition), due))
        return self.work(name)

    def work(self, name):
        with self.store.connection() as conn:
            row = conn.execute("SELECT * FROM works WHERE id=?", (name,)).fetchone()
        if not row:
            raise ValueError(f"工作不存在：{name}")
        return {**dict(row), "definition": json.loads(row["definition"])}

    def works(self):
        with self.store.connection() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM works ORDER BY created")]
        return [self.work(name) for name in ids]

    def enable(self, name, enabled: bool):
        work = self.work(name)
        definition = work["definition"]
        due = next_slot(definition["cron"], definition["timezone"], time.time()) if enabled and definition["cron"] else None
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if enabled and definition.get("run_at") is not None:
                if conn.execute("SELECT 1 FROM triggers WHERE work_id=? LIMIT 1", (name,)).fetchone():
                    raise ValueError("一次性计划已触发，不能重复启用；请新建计划")
                if definition["run_at"] <= time.time():
                    raise ValueError("一次性计划的时间已过，请新建未来时间的计划")
                due = definition["run_at"]
            conn.execute("UPDATE works SET enabled=?,next_due=? WHERE id=?", (int(enabled), due, name))
        return self.work(name)

    def _trigger(self, conn, work, source, key, payload, now):
        existing = conn.execute("SELECT * FROM triggers WHERE work_id=? AND key=?", (work["id"], key)).fetchone()
        if existing:
            return {**dict(existing), "duplicate": True}
        if not work["enabled"]:
            raise ValueError("工作已暂停")
        definition = json.loads(work["definition"])
        busy = conn.execute("SELECT 1 FROM runs WHERE work_id=? AND status IN ('queued','preparing','running','verifying') LIMIT 1", (work["id"],)).fetchone()
        disposition = "skipped_overlap" if busy and definition["overlap"] == "skip" else "queued"
        run_id = None
        if disposition == "queued":
            if conn.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0] >= 100:
                raise ValueError("待执行队列已满（100）；本次触发未接受")
            spec = definition["spec"]
            if not spec.get("installation_id"):
                installed = conn.execute("SELECT id,root,commit_sha FROM repo_installations WHERE origin=? AND status='ready' AND enabled=1 ORDER BY created DESC", (spec["repo"],)).fetchall()
                for item in installed:
                    receipt = json.loads((Path(item["root"]) / "receipt.json").read_text())
                    if receipt.get("ref") == spec.get("ref") or spec.get("ref") == item["commit_sha"]:
                        spec["installation_id"] = item["id"]
                        break
            spec["activation"] = {"work_id": work["id"], "source": source, "key": key, "payload": payload, "at": now}
            run_id = self.store.insert(conn, spec, dispatched=0, work_id=work["id"])
        conn.execute("INSERT INTO triggers VALUES(?,?,?,?,?,?)", (work["id"], key, now, source, run_id, disposition))
        if definition.get("run_at") is not None:
            conn.execute("UPDATE works SET enabled=0,next_due=NULL WHERE id=?", (work["id"],))
        return {"work_id": work["id"], "key": key, "at": now, "source": source, "run_id": run_id, "disposition": disposition, "duplicate": False}

    def trigger(self, name, *, source="manual", key=None, payload=None):
        if source not in {"manual", "web", "cron"}:
            raise ValueError("未知触发来源")
        key = key or uuid.uuid4().hex
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError("触发 key 长度须为 1–200")
        payload = {} if payload is None else payload
        if not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 64 * 1024:
            raise ValueError("payload 须为不超过 64 KiB 的 JSON 对象")
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            work = conn.execute("SELECT * FROM works WHERE id=?", (name,)).fetchone()
            if not work:
                raise ValueError(f"工作不存在：{name}")
            return self._trigger(conn, work, source, key, payload, time.time())

    def triggers(self, name):
        self.work(name)
        with self.store.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM triggers WHERE work_id=? ORDER BY at DESC LIMIT 100", (name,))]

    def cron_tick(self, now=None):
        now = time.time() if now is None else now
        results = []
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            works = conn.execute("SELECT * FROM works WHERE enabled=1 AND next_due<=?", (now,)).fetchall()
            for work in works:
                definition = json.loads(work["definition"])
                # Coalesce missed slots into one durable activation after downtime.
                results.append(self._trigger(conn, work, "cron", "cron:" + str(int(work["next_due"])),
                                             {"scheduled_at": work["next_due"], "coalesced_until": now}, now))
                due = None if definition.get("run_at") is not None else next_slot(definition["cron"], definition["timezone"], now)
                conn.execute("UPDATE works SET next_due=? WHERE id=?", (due, work["id"]))
        return results

    def dispatch(self):
        self.service.reconcile()
        launched = []
        while True:
            with self.store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                count = conn.execute("SELECT count(*) FROM runs WHERE status IN ('preparing','running','verifying') OR (status='queued' AND dispatched=1)").fetchone()[0]
                if count >= self.service.config.max_concurrent:
                    break
                row = conn.execute("SELECT id FROM runs WHERE status='queued' AND dispatched=0 AND cancel_requested=0 ORDER BY created,id LIMIT 1").fetchone()
                if not row:
                    break
                run_id = row[0]
                conn.execute("UPDATE runs SET dispatched=1,updated=? WHERE id=?", (time.time(), run_id))
            # A crash here produces interrupted after grace, never an unknown replay.
            launched.append(self.service._launch(self.store.get(run_id))["id"])
        return launched

    def tick(self):
        if time.monotonic() - self.last_service_check > 15:
            from .repositories import Repositories
            Repositories(self.service.config).recover()
            self.last_service_check = time.monotonic()
        # Drain before cron: a full queue must never block its own dispatcher.
        launched = self.dispatch()
        triggered = self.cron_tick()
        return {"triggered": triggered, "dispatched": launched + self.dispatch()}

    def run(self):
        import logging
        while not self.stop_event.is_set():
            try:
                self.tick()
                self.last_tick_at = time.time()
            except Exception:
                self.last_error_at = time.time()
                logging.exception("scheduler tick failed; durable queue retained")
            self.stop_event.wait(0.5)
