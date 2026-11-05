"""Live HTTP + real cron + CLI inference acceptance (uses configured model credentials).

Run create_scheduled_demo.py, start `agent-base serve`, then execute this script.
The temporary cron work is paused immediately after its first trigger.
"""
import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from agent_base.config import Config
from agent_base.processes import alive

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:8765")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
config = Config.load(os.environ.get("AGENT_BASE_CONFIG", str(root / "agent-base.toml")))
repo = root / ".demo/scheduled/repo"
source = root / ".demo/scheduled/source"
prefix = "accept-" + uuid.uuid4().hex[:8]
headers = {}
if os.environ.get("AGENT_BASE_WEB_TOKEN"):
    headers["Authorization"] = "Bearer " + os.environ["AGENT_BASE_WEB_TOKEN"]
client = httpx.Client(base_url=args.url, headers=headers, timeout=10, trust_env=False)
before = hashlib.sha256((source / "metrics.json").read_bytes()).hexdigest()


def call(method, path, body=None, extra=None):
    response = client.request(method, "/api/" + path, json=body, headers=extra)
    response.raise_for_status()
    return response.json()


cron_name = prefix + "-cron"
try:
    web_name = prefix + "-web"
    common = {"repo": str(repo), "workspace_source": str(source), "timeout_seconds": 120}
    call("POST", "works", {**common, "name": web_name})
    cron_work = call("POST", "works", {**common, "name": cron_name, "cron": "* * * * *"})
    call("POST", "works", {**common, "name": prefix + "-timeout", "timeout_seconds": 5,
                          "task": "先通过 shell 执行 python -c 'import time; time.sleep(60)'，然后生成报告。"})
    request = {"payload": {"period": "demo", "label": "real-http"}}
    web = call("POST", "works/" + web_name + "/trigger", request, {"Idempotency-Key": "click-once"})
    duplicate = call("POST", "works/" + web_name + "/trigger", request, {"Idempotency-Key": "click-once"})
    assert duplicate["run_id"] == web["run_id"] and duplicate["duplicate"]
    timeout = call("POST", "works/" + prefix + "-timeout/trigger", {"payload": {}})
    ids = {"web": web["run_id"], "timeout": timeout["run_id"]}
    print(json.dumps({"runs": ids, "cron_due": cron_work["next_due"]}), flush=True)
    results = {}
    observed = {}
    until = time.time() + 240
    while time.time() < until:
        if "cron" not in ids:
            triggers = call("GET", "works/" + cron_name + "/triggers")
            if triggers:
                ids["cron"] = triggers[0]["run_id"]
                assert ids["cron"] and triggers[0]["source"] == "cron"
                call("POST", "works/" + cron_name + "/pause", {})
                print(json.dumps({"cron_run": ids["cron"], "schedule_paused": True}), flush=True)
        for label, run_id in list(ids.items()):
            if label in results:
                continue
            job = call("GET", "runs/" + run_id)
            if observed.get(label) != (job["status"], job["session_id"]):
                observed[label] = (job["status"], job["session_id"])
                print(json.dumps({"label": label, "status": job["status"], "session_id": job["session_id"]}), flush=True)
            if job["status"] in {"succeeded", "failed", "timed_out", "interrupted", "cancelled"}:
                results[label] = job
        if len(results) == 3:
            break
        time.sleep(0.5)
    assert len(results) == 3, {label: value[0] for label, value in observed.items()}
    for label in ("web", "cron"):
        job = results[label]
        assert job["status"] == "succeeded", job
        assert job["result"]["verification"] == "checks_passed"
        assert not Path(job["workspace"]).exists()
        report = json.loads(Path(job["result"]["artifacts"][0]["path"]).read_text())
        assert report["total_requests"] == 230 and report["total_errors"] == 4
        assert report["trigger_source"] == label
    timed = results["timeout"]
    assert timed["status"] == "timed_out", timed
    assert Path(timed["workspace"]).is_dir()
    assert timed["session_id"]  # captured even though final output never arrived
    events = call("GET", "runs/" + timed["id"] + "/events")
    with (Path(timed["root"]) / "runtime.jsonl").open() as stream:
        assert json.loads(next(stream))["type"] == "session"
    process = next(event["data"] for event in events if event["type"] == "runtime.process_started")
    assert not alive(process["pid"], process["birth"])
    assert len({value["session_id"] for value in results.values()}) == 3
    assert hashlib.sha256((source / "metrics.json").read_bytes()).hexdigest() == before
    assert not subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]).strip()
    target = config.data / "validations" / prefix
    target.mkdir(parents=True)
    summary = {"kind": "live HTTP + wall-clock cron + real dsh", "results": results,
               "idempotency_passed": True, "source_unchanged": True, "cron_paused": True,
               "checked_at": time.time()}
    (target / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({"validation": "passed", "receipt": str(target / "result.json")}), flush=True)
finally:
    # Do not leave the acceptance schedule active, including on assertion failure.
    try:
        call("POST", "works/" + cron_name + "/pause", {})
    except Exception:
        pass
    client.close()
