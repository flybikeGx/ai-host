"""Installation admission must serialize distinct callers sharing SQLite."""
import multiprocessing
from pathlib import Path

import pytest

from agent_base.service_store import ServiceStore


def submit_from_process(data, barrier, results, key):
    store = ServiceStore(Path(data))
    barrier.wait(timeout=15)
    try:
        operation = store.create_operation(deployment_id="shared", kind="install", idempotency_key=key)
        results.put({"id": operation["id"], "duplicate": operation.get("duplicate", False)})
    except ValueError as error:
        results.put({"conflict": str(error)})


@pytest.mark.parametrize("same_key", [False, True])
def test_install_admission_across_processes(tmp_path, same_key):
    ServiceStore(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    workers = [ctx.Process(target=submit_from_process,
                           args=(str(tmp_path), barrier, results, "retry" if same_key else f"request-{i}"))
               for i in range(2)]
    try:
        for worker in workers:
            worker.start()
        answers = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        results.close()
        results.join_thread()
    accepted = [answer for answer in answers if "id" in answer]
    if same_key:
        assert len(accepted) == 2, answers
        assert accepted[0]["id"] == accepted[1]["id"]
        assert sum(answer["duplicate"] for answer in accepted) == 1
    else:
        assert len(accepted) == 1, answers
        assert "进行中的安装" in next(answer["conflict"] for answer in answers if "conflict" in answer)
    assert len(ServiceStore(tmp_path).operations()) == 1


def test_idempotent_terminal_retry_during_another_install(tmp_path):
    store = ServiceStore(tmp_path)
    first = store.create_operation(deployment_id="shared", kind="install", idempotency_key="first")
    store.update_operation(first["id"], status="succeeded")
    second = store.create_operation(deployment_id="shared", kind="install", idempotency_key="second")
    retry = store.create_operation(deployment_id="shared", kind="install", idempotency_key="first")
    assert retry["id"] == first["id"] and retry["duplicate"] and retry["status"] == "succeeded"
    with pytest.raises(ValueError, match=second["id"]):
        store.create_operation(deployment_id="shared", kind="install", idempotency_key="third")
    # An unrelated deployment does not share this admission slot.
    assert store.create_operation(deployment_id="other", kind="install")["status"] == "queued"


def test_active_install_is_not_hidden_by_recent_history(tmp_path):
    store = ServiceStore(tmp_path)
    active = store.create_operation(deployment_id="shared", kind="install")
    for _ in range(25):
        store.create_operation(deployment_id="shared", kind="publish", status="succeeded")
    assert store.active_install("shared")["id"] == active["id"]
    assert store.active_install("shared", operation_id=active["id"]) is None
    with pytest.raises(ValueError, match=active["id"]):
        store.create_operation(deployment_id="shared", kind="install")
