import json
import time
from pathlib import Path

import pytest

from agent_base.service import Service
from agent_base.store import Store


def finish(service, run_id, seconds=15):
    job = service.wait(run_id, seconds)
    assert job["status"] in {"succeeded", "failed", "prepared", "cancelled", "timed_out"}, job
    return job


def test_repository_to_verified_result(config, repo):
    service = Service(config)
    job = service.submit(str(repo), "write report\n中文 & $(literal)", model="another-model", effort="low")
    final = finish(service, job["id"])
    assert final["status"] == "succeeded", final
    root = Path(final["root"])
    captured = json.loads((root / "repo/captured-input.json").read_text())
    assert "仓库规则" in captured["prompt"]
    assert "中文 & $(literal)" in captured["prompt"]
    assert captured["selection"] == {"provider": "deepseek-official", "model": "another-model", "reasoningEffort": "low"}
    assert captured["home"] == str(root / "dsh-home")
    assert (root / "receipt.json").is_file()
    assert len(json.loads((root / "receipt.json").read_text())["commit"]) == 40
    assert final["result"]["artifacts"][0]["sha256"]
    assert final["result"]["verification"] == "structural_only"


def test_install_then_start_once(config, repo):
    service = Service(config)
    job = service.submit(str(repo), install_only=True)
    prepared = finish(service, job["id"])
    assert prepared["status"] == "prepared"
    root = Path(prepared["root"])
    assert not (root / "runtime.jsonl").exists()
    service.start_prepared(job["id"], "write report")
    assert finish(service, job["id"])["status"] == "succeeded"
    with pytest.raises(ValueError):
        service.start_prepared(job["id"], "run again")


@pytest.mark.parametrize("task,message", [("FIXTURE_FAIL", "退出码 9"), ("FIXTURE_NO_ARTIFACT", "产物不存在")])
def test_failure_is_not_success(config, repo, task, message):
    service = Service(config)
    result = finish(service, service.submit(str(repo), task)["id"])
    assert result["status"] == "failed"
    assert message in result["error"]


def test_cancel_running_and_queued_jobs(config, repo):
    config.max_concurrent = 1
    service = Service(config)
    first = service.submit(str(repo), "FIXTURE_SLEEP")
    second = service.submit(str(repo), "write report")
    for _ in range(60):
        if service.get(first["id"])["status"] == "running":
            break
        time.sleep(0.1)
    assert service.get(second["id"])["status"] == "queued"
    service.cancel(second["id"])
    service.cancel(first["id"])
    assert finish(service, second["id"])["status"] == "cancelled"
    assert finish(service, first["id"])["status"] == "cancelled"
    from agent_base.processes import alive
    stopped = service.store.get(first["id"])
    assert not alive(stopped["process_pid"], stopped["process_birth"])


def test_actual_parallel_jobs_keep_separate_workspaces(config, repo):
    service = Service(config)
    jobs = [service.submit(str(repo), "FIXTURE_SLEEP") for _ in range(3)]
    until = time.time() + 12
    saw_parallel = False
    while time.time() < until:
        states = [service.get(job["id"])["status"] for job in jobs]
        active = sum(s in {"running", "preparing", "verifying"} for s in states)
        assert active <= 2
        if states.count("running") == 2:
            saw_parallel = True
            assert states.count("queued") == 1
            break
        time.sleep(0.1)
    for job in jobs:
        service.cancel(job["id"])
    for job in jobs:
        finish(service, job["id"])
    assert saw_parallel
    assert len({job["root"] for job in jobs}) == 3


def test_timeout_cannot_report_success(config, repo):
    config.runtime["timeout_seconds"] = 1.5
    service = Service(config)
    result = finish(service, service.submit(str(repo), "FIXTURE_SLEEP")["id"])
    assert result["status"] == "timed_out"


def test_store_atomic_claim_and_no_unknown_replay(config):
    service = Service(config)
    store = service.store
    one, two = store.create({}), store.create({})
    assert not store.claim(two["id"], 2)
    assert store.claim(one["id"], 1)
    assert not store.claim(one["id"], 2)
    assert not store.claim(two["id"], 1)
    with store.connection() as conn:
        conn.execute("UPDATE runs SET updated=0,pid=99999999,birth=0 WHERE id=?", (one["id"],))
    service.reconcile()
    assert store.get(one["id"])["status"] == "interrupted"
    assert store.claim(two["id"], 1)


def test_late_reconciliation_cannot_overwrite_completed_run(config, monkeypatch):
    service = Service(config)
    job = service.store.create({})
    with service.store.connection() as conn:
        conn.execute("UPDATE runs SET status='running',updated=0 WHERE id=?", (job["id"],))
    def completed_before_liveness_check(*_):
        service.store.update(job["id"], status="succeeded", result={"text": "done"})
        return False
    monkeypatch.setattr("agent_base.service.alive", completed_before_liveness_check)
    service.reconcile()
    assert service.store.get(job["id"])["status"] == "succeeded"
