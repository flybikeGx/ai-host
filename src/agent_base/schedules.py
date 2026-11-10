"""Graphical schedules bind a ready installation and its frozen entrypoint."""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .package import read_entrypoint
from .repositories import Repositories
from .scheduler import next_slot


class Schedules:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.repositories = Repositories(scheduler.service.config)

    def catalog(self):
        agents = []
        for item in self.repositories.list():
            if item["status"] != "ready":
                continue
            reason = ""
            try:
                entrypoint = read_entrypoint(Path(item["root"]) / "source")
            except ValueError as error:
                entrypoint, reason = None, str(error)
            if not entrypoint:
                reason = reason or "此安装版本未提供 entrypoint.md，请更新并重新安装 Repo"
            if not item["enabled"]:
                reason = "Agent 服务已停用"
            agents.append({"id": item["id"], "name": item["manifest"].get("package", {}).get("name", item["repo_id"]),
                           "commit": item["commit_sha"], "available": bool(entrypoint and item["enabled"]),
                           "reason": reason, "entrypoint_preview": (entrypoint or "")[:4096]})
        runtime = self.scheduler.service.config.runtime
        return {"agents": agents, "defaults": {"model": runtime["model"], "effort": runtime.get("effort", "high")},
                "server_time": time.time()}

    def resolve(self, data, *, now=None):
        now = time.time() if now is None else now
        item = self.repositories.get(data["agent_id"])
        if item["status"] != "ready" or not item["enabled"]:
            raise ValueError("Agent 尚未安装就绪或已停用，请选择可用的 Agent")
        if read_entrypoint(Path(item["root"]) / "source") is None:
            raise ValueError("此安装版本缺少 entrypoint.md，不能创建无人值守计划")
        try:
            zone = ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("无效时区") from error
        if not re.fullmatch(r"\d{2}:\d{2}", data["time"]):
            raise ValueError("时间须为 HH:MM")
        try:
            clock = datetime.strptime(data["time"], "%H:%M")
        except ValueError as error:
            raise ValueError("无效时间") from error
        frequency = data["frequency"]
        run_at, cron = None, None
        if frequency == "once":
            try:
                local = datetime.strptime(data.get("date") or "", "%Y-%m-%d").replace(hour=clock.hour, minute=clock.minute)
            except ValueError as error:
                raise ValueError("请选择有效日期") from error
            first, second = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
            run_at = first.timestamp()
            if datetime.fromtimestamp(run_at, zone).replace(tzinfo=None) != local:
                raise ValueError("该时间位于夏令时跳变区间，请选择其他时间")
            if first.utcoffset() != second.utcoffset():
                raise ValueError("该时间因夏令时出现两次，请选择无歧义的时间")
            if run_at <= now:
                raise ValueError("执行时间必须晚于当前时间")
            times = [run_at]
        else:
            if frequency not in {"daily", "weekly"}:
                raise ValueError("不支持的重复方式")
            weekdays = data.get("weekdays", [])
            if frequency == "weekly" and (not weekdays or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in weekdays)):
                raise ValueError("每周计划至少选择一个有效星期")
            days = ",".join(str(d) for d in sorted(set(weekdays))) if frequency == "weekly" else "*"
            cron = f"{clock.minute} {clock.hour} * * {days}"
            times, after = [], now
            for _ in range(3):
                after = next_slot(cron, data["timezone"], after)
                times.append(after)
        return item, {"run_at": run_at, "cron": cron, "next_times": times,
                      "timezone": data["timezone"], "frequency": frequency}

    def preview(self, data):
        return self.resolve(data)[1]

    def create(self, data):
        item, timing = self.resolve(data)
        title = (data.get("title") or item["manifest"].get("package", {}).get("name", "Agent") + " 定时任务").strip()
        if not title or len(title) > 100:
            raise ValueError("计划名称须为 1–100 字符")
        metadata = {key: data.get(key) for key in ("agent_id", "frequency", "date", "time", "weekdays", "timezone")}
        metadata.update(title=title, agent_name=item["manifest"].get("package", {}).get("name", "Agent"), commit=item["commit_sha"])
        work = self.scheduler.register("schedule-" + uuid.uuid4().hex, item["origin"], ref=item["commit_sha"],
            installation_id=item["id"], task=data.get("task"), model=data.get("model"), effort=data.get("effort"),
            timeout_seconds=data.get("timeout_seconds", 1800), cron=timing["cron"], run_at=timing["run_at"],
            timezone=data["timezone"], schedule=metadata)
        return self.summary(work)

    def get(self, name):
        work = self.scheduler.work(name)
        if not work["definition"].get("schedule"):
            raise ValueError("定时计划不存在")
        return work

    def summary(self, work):
        definition = work["definition"]
        with self.scheduler.store.connection() as conn:
            latest = conn.execute("SELECT id,status,created,finished_at FROM runs WHERE work_id=? ORDER BY created DESC,id DESC LIMIT 1", (work["id"],)).fetchone()
            consumed = conn.execute("SELECT 1 FROM triggers WHERE work_id=? LIMIT 1", (work["id"],)).fetchone()
        once = definition.get("run_at") is not None
        return {"id": work["id"], **definition["schedule"], "enabled": bool(work["enabled"]),
                "next_due": work["next_due"], "created": work["created"], "consumed": bool(once and consumed),
                "latest_run": dict(latest) if latest else None}

    def list(self):
        return [self.summary(work) for work in reversed(self.scheduler.works()) if work["definition"].get("schedule")]

    def enable(self, name, enabled):
        self.get(name)
        return self.summary(self.scheduler.enable(name, enabled))

    def runs(self, name):
        self.get(name)
        with self.scheduler.store.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT id,status,created,started_at,finished_at,error FROM runs WHERE work_id=? ORDER BY created DESC,id DESC LIMIT 20", (name,))]
