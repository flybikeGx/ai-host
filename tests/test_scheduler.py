import json
import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_base.processes import alive
from agent_base.scheduler import Scheduler, copy_inputs
from agent_base.web import create_app


def wait_for(scheduler, run_id, states, seconds=15):
    until = time.time() + seconds
    while time.time() < until:
        job = scheduler.service.get(run_id)
        if job["status"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(scheduler.store.get(run_id))


def test_web_durable_queue_idempotency_snapshot_cleanup(config, repo, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.json").write_text('{"value": 42}')
    (source / ".env").write_text("DO_NOT_COPY=secret")
    app = create_app(config, background=False)
    scheduler = app.state.scheduler
    with TestClient(app) as client:
        body = {"name": "web-report", "repo": str(repo), "workspace_source": str(source)}
        assert client.post("/api/works", json=body).status_code == 201
        request = {"payload": {"period": "2026-11"}}
        first = client.post("/api/works/web-report/trigger", headers={"Idempotency-Key": "click-1"}, json=request).json()
        again = client.post("/api/works/web-report/trigger", headers={"Idempotency-Key": "click-1"}, json=request).json()
        assert again["duplicate"] and first["run_id"] == again["run_id"]
        run_id = first["run_id"]
        # No worker running: the job survives reconcile/restart and does not spawn.
        with scheduler.store.connection() as conn:
            conn.execute("UPDATE runs SET updated=0 WHERE id=?", (run_id,))
        restarted = Scheduler(config)
        assert restarted.service.get(run_id)["status"] == "queued"
        assert restarted.store.get(run_id)["pid"] is None
        assert restarted.dispatch() == [run_id]
        final = wait_for(restarted, run_id, {"succeeded", "failed"})
        assert final["status"] == "succeeded", final
        assert final["session_id"] and final["session_status"] == "finished"
        assert final["started_at"] <= final["heartbeat"] <= final["finished_at"]
        assert not Path(final["workspace"]).exists()
        assert final["result"]["workspace_retained"] is False
        package = Path(final["root"]) / "package"
        assert json.loads((package / "input/metrics.json").read_text())["value"] == 42
        assert not (package / "input/.env").exists()
        activation = json.loads((package / "activation.json").read_text())
        assert activation["source"] == "web" and activation["payload"] == request["payload"]
        assert (source / "metrics.json").read_text() == '{"value": 42}'
        assert Path(final["result"]["artifacts"][0]["path"]).is_file()
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded"
        assert client.get(f"/api/runs/{run_id}/events").status_code == 200


def test_atomic_cron_coalesces_and_skips_overlap(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("clock", str(repo), cron="* * * * *", now=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: Scheduler(config).cron_tick(now=600), range(2)))
    assert sum(len(r) for r in results) == 1
    first = next(r[0] for r in results if r)
    assert first["disposition"] == "queued"
    assert scheduler.work("clock")["next_due"] == 660
    skipped = scheduler.cron_tick(now=660)[0]
    assert skipped["disposition"] == "skipped_overlap" and skipped["run_id"] is None
    assert len(scheduler.triggers("clock")) == 2
    assert len(scheduler.store.list()) == 1
    scheduler.service.cancel(first["run_id"])
    next_run = scheduler.cron_tick(now=720)[0]
    assert next_run["run_id"] != first["run_id"]
    scheduler.service.cancel(next_run["run_id"])


def test_competing_dispatchers_respect_capacity_and_timeout(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("slow", str(repo), task="FIXTURE_SLEEP", timeout_seconds=3, overlap="allow")
    ids = [scheduler.trigger("slow")["run_id"] for _ in range(3)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        launched = list(pool.map(lambda _: Scheduler(config).dispatch(), range(2)))
    assert len({run_id for group in launched for run_id in group}) == 2
    assert scheduler.store.get(ids[2])["dispatched"] == 0
    running = wait_for(scheduler, ids[0], {"running"})
    until = time.time() + 2
    while not scheduler.store.get(ids[0])["session_id"] and time.time() < until:
        time.sleep(0.05)
    session = scheduler.store.get(ids[0])
    assert session["session_id"] and session["session_status"] == "running"
    child_pid, child_birth = session["process_pid"], session["process_birth"]
    assert alive(child_pid, child_birth)
    for run_id in ids[:2]:
        result = wait_for(scheduler, run_id, {"timed_out"})
        assert result["session_status"] == "timed_out"
        assert Path(result["workspace"]).is_dir()  # failed workspace retained
        assert result["finished_at"] and result["session_id"]
    assert not alive(child_pid, child_birth)
    assert scheduler.dispatch() == [ids[2]]
    scheduler.service.cancel(ids[2])
    wait_for(scheduler, ids[2], {"cancelled"})


def test_atomic_web_keys_and_pause(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("once", str(repo), overlap="allow")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Scheduler(config).trigger("once", source="web", key="request-42"), range(4)))
    assert len({r["run_id"] for r in results}) == 1
    assert sum(not r["duplicate"] for r in results) == 1
    scheduler.enable("once", False)
    with pytest.raises(ValueError, match="暂停"):
        scheduler.trigger("once")
    assert scheduler.trigger("once", key="request-42")["duplicate"]
    run_id = results[0]["run_id"]
    assert scheduler.service.cancel(run_id)["status"] == "cancelled"
    assert scheduler.dispatch() == []


def test_web_auth_and_cross_origin_writes(config, repo, monkeypatch):
    monkeypatch.setenv("AGENT_BASE_WEB_TOKEN", "fixture-only-token")
    with TestClient(create_app(config, background=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/works").status_code == 401
        auth = {"Authorization": "Bearer fixture-only-token"}
        assert client.get("/api/works", headers=auth).status_code == 200
        assert client.post("/api/works", headers={**auth, "Origin": "https://untrusted.invalid"},
                           json={"name": "bad", "repo": str(repo)}).status_code == 403
        assert client.get("/api/works", headers={**auth, "Host": "untrusted.invalid"}).status_code == 400


def test_inputs_reject_symlinks(config, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not copy")
    (source / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        copy_inputs(source, tmp_path / "copy", time.time() + 10, lambda: False)


def test_dead_supervisor_is_not_replayed(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("dead", str(repo))
    run_id = scheduler.trigger("dead")["run_id"]
    with scheduler.store.connection() as conn:
        conn.execute("UPDATE runs SET status='running',dispatched=1,updated=0,pid=99999999,birth=0 WHERE id=?", (run_id,))
    scheduler.service.reconcile()
    assert scheduler.store.get(run_id)["status"] == "interrupted"
    assert scheduler.dispatch() == []


def test_background_web_lifespan_drains_queue(config, repo):
    app = create_app(config)
    with TestClient(app) as client:
        client.post("/api/works", json={"name": "automatic", "repo": str(repo)})
        trigger = client.post("/api/works/automatic/trigger", json={}).json()
        final = wait_for(app.state.scheduler, trigger["run_id"], {"succeeded", "failed"})
        assert final["status"] == "succeeded", final
    assert app.state.scheduler.stop_event.is_set()


@pytest.mark.skipif(os.name == "nt", reason="POSIX forced-crash scenario")
def test_owner_crash_stops_recorded_cli_and_retains_workspace(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("crash", str(repo), task="FIXTURE_SLEEP")
    run_id = scheduler.trigger("crash")["run_id"]
    scheduler.dispatch()
    wait_for(scheduler, run_id, {"running"})
    until = time.time() + 3
    while time.time() < until:
        job = scheduler.store.get(run_id)
        if job["session_id"]:
            break
        time.sleep(0.05)
    assert job["session_id"]
    child = (job["process_pid"], job["process_birth"])
    os.kill(job["pid"], signal.SIGKILL)
    until = time.time() + 3
    while alive(job["pid"], job["birth"]) and time.time() < until:
        time.sleep(0.05)
    with scheduler.store.connection() as conn:
        conn.execute("UPDATE runs SET updated=0 WHERE id=?", (run_id,))
    scheduler.service.reconcile()
    final = scheduler.store.get(run_id)
    assert final["status"] == "interrupted"
    assert final["session_status"] == "interrupted"
    assert not alive(*child)
    assert Path(final["workspace"]).is_dir()
    assert scheduler.dispatch() == []


def test_full_queue_still_drains_when_cron_is_due(config, repo):
    scheduler = Scheduler(config)
    scheduler.register("full", str(repo), overlap="allow", cron="* * * * *", now=0)
    ids = [scheduler.trigger("full")["run_id"] for _ in range(100)]
    # Cron cannot append run 101, but must not prevent dispatch of existing work.
    try:
        scheduler.tick()
    except ValueError as error:
        assert "队列已满" in str(error)
    for run_id in ids[:2]:
        assert wait_for(scheduler, run_id, {"succeeded", "failed"})["status"] == "succeeded"
    for run_id in ids[2:]:
        scheduler.service.cancel(run_id)
