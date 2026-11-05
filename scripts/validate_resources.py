"""Real dsh acceptance: three namespaces, stable skills and persistent MCP processes."""
import hashlib
import json
import math
import time
from pathlib import Path

from agent_base.config import Config
from agent_base.path_lint import check_prompts, lint_text
from agent_base.repositories import Repositories
from agent_base.service import Service

project = Path(__file__).resolve().parents[1]
config = Config.load()
repositories = Repositories(config)
service = Service(config)
installed = {}
before = {}
for name in ("finance", "inventory", "research"):
    repo = project / ".demo/resources" / name
    assert not check_prompts(repo)
    item = repositories.install(str(repo))
    installed[name] = item
    skill = Path(item["root"]) / "skills/analysis/SKILL.md"
    before[name] = {"health": repositories.health(item), "skill_hash": hashlib.sha256(skill.read_bytes()).hexdigest(),
                    "skill_inode": skill.stat().st_ino}
    print(json.dumps({"repo": name, "installation_id": item["id"], "namespace": item["namespace"], "service_pid": item["pid"]}), flush=True)

task = "必须使用本命名空间的 read_skill 工具读取 analysis，然后调用 domain__analyze 分析 [2, 3, 5]，最后按 skill 用 write_report 保存完整结果。"
jobs = {label: service.submit(installed[name]["origin"], task) for label, name in (
    ("finance-1", "finance"), ("finance-2", "finance"), ("inventory", "inventory"), ("research", "research"))}
finals = {}
observed = {}
until = time.time() + 300
while time.time() < until:
    for label, job in jobs.items():
        final = service.get(job["id"])
        if observed.get(label) != final["status"]:
            print(json.dumps({"label": label, "run_id": job["id"], "status": final["status"]}), flush=True)
            observed[label] = final["status"]
        if final["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
            finals[label] = final
    if len(finals) == len(jobs):
        break
    time.sleep(0.5)
assert len(finals) == len(jobs), observed
reports = {}
for label, final in finals.items():
    name = "finance" if label.startswith("finance") else label
    assert final["status"] == "succeeded", final
    assert final["result"]["verification"] == "checks_passed"
    prompt = (Path(final["root"]) / "prompt.txt").read_text()
    assert not lint_text(prompt, "prompt")
    assert installed[name]["namespace"] in prompt
    for other, item in installed.items():
        if other != name:
            assert item["namespace"] not in prompt
    patch = (Path(final["root"]) / "runtime.patch.yml").read_text()
    assert installed[name]["namespace"] in patch
    assert ".agent/skills/analysis/SKILL.md" in prompt
    names = {e["data"]["tool"] for e in service.store.events(final["id"]) if e["type"] == "repo.tool_finished"}
    assert {"read_skill", "domain__analyze", "write_report"} <= names, names
    report = json.loads(Path(final["result"]["artifacts"][0]["path"]).read_text())
    reports[label] = report
    assert report["analysis"]["domain"] == name
    assert report["namespace"] == installed[name]["namespace"]
    assert math.isclose(report["analysis"]["value"], {"finance": 10, "inventory": 30, "research": 10 / 3}[name])
    assert report["skill_marker"] == name.upper() + "_SKILL_OK"
assert reports["finance-1"]["analysis"]["pid"] == reports["finance-2"]["analysis"]["pid"]
assert reports["finance-1"]["analysis"]["boot_id"] == reports["finance-2"]["analysis"]["boot_id"]
assert reports["finance-1"]["analysis"]["call_number"] != reports["finance-2"]["analysis"]["call_number"]
for name, item in installed.items():
    current = repositories.get(item["id"])
    health = repositories.health(current)
    assert health and health["boot_id"] == before[name]["health"]["boot_id"]
    skill = Path(item["root"]) / "skills/analysis/SKILL.md"
    assert skill.stat().st_ino == before[name]["skill_inode"]
    assert hashlib.sha256(skill.read_bytes()).hexdigest() == before[name]["skill_hash"]
    assert (Path(item["root"]) / "source/installation-count.txt").read_text() == "1"
target = config.data / "validations" / ("resources-" + str(time.time_ns()))
target.mkdir(parents=True)
(target / "result.json").write_text(json.dumps({"kind": "real dsh + persistent MCP namespaces", "runs": finals,
    "reports": reports, "installations": installed, "services_remained_running": True, "skills_unchanged": True}, ensure_ascii=False, indent=2))
print(json.dumps({"validation": "passed", "receipt": str(target / "result.json")}), flush=True)
