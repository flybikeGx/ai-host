import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_base.conversations import Conversations
from agent_base.package import load_package, read_entrypoint
from agent_base.path_lint import check_prompts
from agent_base.reports import parse_agent_report
from agent_base.scheduler import Scheduler
from agent_base.service import Service
from agent_base.web import create_app


def commit(repo):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "entrypoint contract"], check=True)


def entry_repo(repo):
    (repo / "agent.yaml").write_text("schema_version: 1\n")
    (repo / "entrypoint.md").write_text("ENTRYPOINT_OBJECTIVE：从 input 读取数据并生成 artifacts/report.txt。")
    commit(repo)
    return repo


def test_entrypoint_only_run_and_report(config, repo):
    service = Service(config)
    final = service.wait(service.submit(str(entry_repo(repo)))["id"], 15)
    assert final["status"] == "succeeded", final
    root = Path(final["root"])
    prompt = (root / "prompt.txt").read_text()
    assert prompt.index("仓库规则") < prompt.index("ENTRYPOINT_OBJECTIVE") < prompt.index("<agent_base_report_contract>")
    meta = json.loads((root / "prompt-contract.json").read_text())
    assert meta["entrypoint_sha256"] and meta["prompt_sha256"] and meta["entrypoint"] == "entrypoint.md"
    assert final["result"]["agent_report"]["status"] == "completed"
    receipt = json.loads((root / "report.json").read_text())
    assert receipt["status"] == "succeeded" and receipt["agent_report"] == final["result"]["agent_report"]
    assert receipt["verification"] == "structural_only"
    # Declared outputs are archived even when not predeclared in the manifest.
    assert receipt["artifacts"][0]["relative_path"] == "artifacts/report.txt"
    assert Path(receipt["artifacts"][0]["path"]).is_file()


def test_entrypoint_cron_input_and_report_endpoint(config, repo, tmp_path):
    scheduler = Scheduler(config)
    source = tmp_path / "source"
    source.mkdir()
    (source / "metrics.json").write_text('{"requests":3}')
    scheduler.register("entry-cron", str(entry_repo(repo)), workspace_source=str(source), cron="* * * * *")
    work = scheduler.work("entry-cron")
    scheduler.cron_tick(now=work["next_due"] + 1)
    trigger = scheduler.triggers("entry-cron")[0]
    scheduler.dispatch()
    final = scheduler.service.wait(trigger["run_id"], 15)
    assert final["status"] == "succeeded", final
    receipt = json.loads((Path(final["root"]) / "report.json").read_text())
    assert receipt["activation"]["source"] == "cron"
    assert "activation.json" in (Path(final["root"]) / "prompt.txt").read_text()
    assert not Path(final["workspace"]).exists()
    with TestClient(create_app(config, background=False)) as client:
        response = client.get(f"/api/runs/{final['id']}/report")
        assert response.status_code == 200
        assert response.json()["run_id"] == final["id"]
        assert response.json()["artifacts"][0]["sha256"]


def test_entrypoint_survives_prepare_and_frozen_source(config, repo):
    service = Service(config)
    prepared = service.wait(service.submit(str(entry_repo(repo)), install_only=True)["id"], 15)
    assert prepared["status"] == "prepared"
    (repo / "entrypoint.md").write_text("SOURCE_CHANGED_AFTER_INSTALL")
    service.start_prepared(prepared["id"], "本次补充参数")
    final = service.wait(prepared["id"], 15)
    assert final["status"] == "succeeded", final
    prompt = (Path(final["root"]) / "prompt.txt").read_text()
    assert "ENTRYPOINT_OBJECTIVE" in prompt and "SOURCE_CHANGED_AFTER_INSTALL" not in prompt
    assert prompt.index("ENTRYPOINT_OBJECTIVE") < prompt.index("本次补充参数") < prompt.index("<agent_base_report_contract>")


def test_v2_entrypoint_and_chat_remains_interactive(config, resource_repos, repositories):
    repo = resource_repos["finance"]
    (repo / "entrypoint.md").write_text("ENTRYPOINT_BATCH_ONLY：执行固定分析并生成 artifacts/report.json。")
    commit(repo)
    installed = repositories.install(str(repo))
    before = repositories.health(installed)["boot_id"]
    service = Service(config)
    final = service.wait(service.submit(str(repo))["id"], 20)
    assert final["status"] == "succeeded", final
    prompt = (Path(final["root"]) / "prompt.txt").read_text()
    assert "ENTRYPOINT_BATCH_ONLY" in prompt and "<agent_base_report_contract>" in prompt
    assert json.loads((Path(final["root"]) / "receipt.json").read_text())["entrypoint_sha256"]
    chats = Conversations(config)
    chat = chats.create(installed["id"])
    turn = chats.send(chat["id"], "普通交互请求", "entrypoint-chat")
    until = time.time() + 20
    while time.time() < until and chats.turn(turn["id"])["status"] in {"queued", "running"}:
        time.sleep(0.1)
    done = chats.turn(turn["id"])
    assert done["status"] == "succeeded", done
    chat_prompt = (Path(done["root"]) / "prompt.txt").read_text()
    assert "ENTRYPOINT_BATCH_ONLY" not in chat_prompt and "<agent_base_report_contract>" not in chat_prompt
    assert done["assistant_text"] == "fixture completed"
    assert repositories.health(repositories.get(installed["id"]))["boot_id"] == before


@pytest.mark.parametrize("task,message", [
    ("REPORT_INVALID", "结构化报告契约"),
    ("REPORT_BLOCKED", "blocked"),
    ("REPORT_MISSING", "产物不存在"),
    ("REPORT_FAILED_CHECK", "包含失败检查"),
])
def test_bad_or_incomplete_report_is_not_success(config, repo, task, message):
    service = Service(config)
    final = service.wait(service.submit(str(entry_repo(repo)), task)["id"], 15)
    assert final["status"] == "failed" and message in final["error"], final
    assert Path(final["workspace"]).is_dir()
    report = json.loads((Path(final["root"]) / "report.json").read_text())
    assert report["status"] == "failed" and report["verification"] == "not_verified"
    if task == "REPORT_BLOCKED":
        assert report["agent_report"]["status"] == "blocked"


def test_agent_self_check_does_not_upgrade_verification(config, repo):
    service = Service(config)
    final = service.wait(service.submit(str(entry_repo(repo)), "REPORT_SELF_CHECK REPORT_ONLY_EXTRA")["id"], 15)
    assert final["status"] == "succeeded", final
    assert final["result"]["verification"] == "structural_only"
    assert {p["relative_path"] for p in final["result"]["artifacts"]} == {"artifacts/report.txt", "artifacts/extra.txt"}


def test_terminal_without_agent_report_and_prepared_endpoint(config, repo):
    service = Service(config)
    config.runtime["timeout_seconds"] = 1
    final = service.wait(service.submit(str(entry_repo(repo)), "FIXTURE_SLEEP")["id"], 10)
    report = json.loads((Path(final["root"]) / "report.json").read_text())
    assert report["status"] == "timed_out" and report["agent_report"] is None
    scheduler = Scheduler(config)
    scheduler.register("queued", str(repo))
    queued = scheduler.trigger("queued")["run_id"]
    service.cancel(queued)
    assert json.loads((Path(service.get(queued)["root"]) / "report.json").read_text())["status"] == "cancelled"
    config.runtime["timeout_seconds"] = 15
    prepared = service.wait(service.submit(str(repo), install_only=True)["id"], 15)
    with TestClient(create_app(config, background=False)) as client:
        assert client.get(f"/api/runs/{prepared['id']}/report").status_code == 409


@pytest.mark.parametrize("entry", ["", "读取 /etc/passwd", "x" * (128 * 1024 + 1)])
def test_bad_entrypoint_rejected(repo, entry):
    (repo / "entrypoint.md").write_text(entry)
    with pytest.raises(ValueError, match="entrypoint"):
        load_package(repo)
    if "/etc/passwd" in entry:
        assert any(i["file"] == "entrypoint.md" for i in check_prompts(repo))


def test_entrypoint_cannot_escape_frozen_repository(repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (repo / "entrypoint.md").symlink_to(outside)
    with pytest.raises(ValueError, match="越界"):
        read_entrypoint(repo)


@pytest.mark.parametrize("path", ["artifacts/../secret", "artifacts/../../outside", "artifacts/\\secret", "/tmp/report", "C:/report"])
def test_report_paths_cannot_escape(repo, path):
    report = {"schema_version": 1, "status": "completed", "summary": "ok", "artifacts": [{"path": path}],
              "checks": [], "issues": [], "next_steps": []}
    with pytest.raises(ValueError):
        parse_agent_report(json.dumps(report), repo)


def test_report_rejects_duplicate_keys_non_json_and_symlink(repo, tmp_path):
    for text in ('{"status":"completed","status":"failed"}', '{"schema_version":NaN}', '```json\n{}\n```', '{} trailing'):
        with pytest.raises(ValueError):
            parse_agent_report(text, repo)
    (repo / "artifacts").symlink_to(tmp_path)
    report = {"schema_version": 1, "status": "completed", "summary": "ok", "artifacts": [{"path": "artifacts/outside"}],
              "checks": [], "issues": [], "next_steps": []}
    with pytest.raises(ValueError, match="越界"):
        parse_agent_report(json.dumps(report), repo)
