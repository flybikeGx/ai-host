import asyncio
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.package import load_package
from agent_base.processes import alive
from agent_base.repositories import Repositories
from agent_base.service import Service


def test_installation_markdown_is_unambiguous(resource_repos):
    root = resource_repos["finance"]
    manifest = load_package(root)
    assert manifest["schema_version"] == 2
    assert manifest["skills"][0]["name"] == "analysis"
    (root / "agent.yaml").write_text("schema_version: 2")
    with pytest.raises(ValueError, match="机器配置来源"):
        load_package(root)
    (root / "agent.yaml").unlink()


def test_absolute_path_text_no_longer_blocks_installation_or_prompt(config, repositories, resource_repos):
    root = resource_repos["finance"]
    (root / "AGENTS.md").write_text("读取 /Users/person/repo/data.json、C:\\work\\data.json 和 ~/notes；这是示例文本。")
    (root / "entrypoint.md").write_text("参考 /etc/hosts 的格式生成 artifacts/report.txt；路径仅用于说明。")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "absolute path text"], check=True)
    manifest = load_package(root)
    assert manifest["schema_version"] == 2
    item = repositories.install(str(root))
    assert item["status"] == "ready"
    service = Service(config)
    final = service.wait(service.submit(str(root))["id"], 25)
    assert final["status"] == "succeeded", final
    prompt = (Path(final["root"]) / "prompt.txt").read_text()
    assert "/Users/person/repo/data.json" in prompt and "参考 /etc/hosts" in prompt


def test_install_once_persistent_mcp_and_stable_skills(repositories, resource_repos):
    item = repositories.install(str(resource_repos["finance"]))
    health = repositories.health(item)
    assert item["status"] == "ready" and health["healthy"]
    root = Path(item["root"])
    skill = root / "skills/analysis/SKILL.md"
    original = (skill.stat().st_ino, skill.read_bytes(), health["boot_id"], item["pid"])
    again = repositories.install(str(resource_repos["finance"]))
    assert again["id"] == item["id"]
    assert repositories.health(again)["boot_id"] == original[2]
    assert again["pid"] == original[3]
    assert skill.stat().st_ino == original[0] and skill.read_bytes() == original[1]
    assert (root / "source/installation-count.txt").read_text() == "1"
    assert set(health["tools"]) == {"domain__analyze", "read_skill", "write_report"}


def test_competing_installs_share_one_service(repositories, resource_repos):
    with ThreadPoolExecutor(max_workers=2) as pool:
        installed = list(pool.map(lambda _: repositories.install(str(resource_repos["finance"])), range(2)))
    assert installed[0]["id"] == installed[1]["id"]
    assert installed[0]["pid"] == installed[1]["pid"]
    assert (Path(installed[0]["root"]) / "source/installation-count.txt").read_text() == "1"


def test_three_namespaces_and_shared_service_across_runs(config, repositories, resource_repos):
    installed = {name: repositories.install(str(repo)) for name, repo in resource_repos.items()}
    service = Service(config)
    jobs = {name: service.submit(str(repo)) for name, repo in resource_repos.items()}
    reports = {}
    for name, job in jobs.items():
        final = service.wait(job["id"], 25)
        assert final["status"] == "succeeded", final
        report = json.loads(Path(final["result"]["artifacts"][0]["path"]).read_text())
        assert report["analysis"]["domain"] == name
        assert report["namespace"] == installed[name]["namespace"]
        assert report["skill_marker"] == name.upper() + "_SKILL_OK"
        assert final["result"]["verification"] == "checks_passed"
        assert not Path(final["workspace"]).exists()
        patch = (Path(final["root"]) / "runtime.patch.yml").read_text()
        assert "projectRootMarkers" in patch and ".agent-base-root" in patch
        reports[name] = report
    assert len({r["analysis"]["pid"] for r in reports.values()}) == 3
    assert len({item["namespace"] for item in installed.values()}) == 3
    # A second instance reuses both the upstream process and its state/counter.
    second = service.wait(service.submit(str(resource_repos["finance"]))["id"], 20)
    assert second["status"] == "succeeded", second
    report = json.loads(Path(second["result"]["artifacts"][0]["path"]).read_text())
    assert report["analysis"]["boot_id"] == reports["finance"]["analysis"]["boot_id"]
    assert report["analysis"]["pid"] == reports["finance"]["analysis"]["pid"]
    assert report["analysis"]["call_number"] == reports["finance"]["analysis"]["call_number"] + 1
    for item in installed.values():
        assert repositories.get(item["id"])["pid"] == item["pid"] and repositories.health(repositories.get(item["id"]))


def test_timeout_and_prepared_start_do_not_stop_repo_service(config, repositories, resource_repos):
    item = repositories.install(str(resource_repos["finance"]))
    health = repositories.health(item)
    service = Service(config)
    config.runtime["timeout_seconds"] = 2
    failed = service.wait(service.submit(str(resource_repos["finance"]), "FIXTURE_SLEEP")["id"], 10)
    assert failed["status"] == "timed_out", failed
    assert repositories.health(repositories.get(item["id"]))["boot_id"] == health["boot_id"]
    config.runtime["timeout_seconds"] = 15
    prepared = service.wait(service.submit(str(resource_repos["finance"]), install_only=True)["id"], 10)
    assert prepared["status"] == "prepared", prepared
    service.start_prepared(prepared["id"])
    finished = service.wait(prepared["id"], 20)
    assert finished["status"] == "succeeded", finished
    assert repositories.health(repositories.get(item["id"]))["boot_id"] == health["boot_id"]


def test_namespace_binding_rejects_foreign_run(config, repositories, resource_repos):
    a = repositories.install(str(resource_repos["finance"]))
    b = repositories.install(str(resource_repos["inventory"]))
    service = Service(config)
    job = service.store.create({"installation_id": a["id"]})
    service.store.update(job["id"], status="running", deadline=time.time() + 15)
    async def exercise():
        async with httpx.AsyncClient(headers=repositories.headers(b, job["id"]), trust_env=False) as client:
            async with streamable_http_client(b["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    answer = await session.call_tool("domain__analyze", {"values": [1]})
                    assert answer.isError and "不属于本命名空间" in str(answer.content)
    asyncio.run(exercise())
    service.store.update(job["id"], status="cancelled")


def test_service_explicit_stop_start_and_recovery(repositories, resource_repos):
    item = repositories.install(str(resource_repos["finance"]))
    old_health = repositories.health(item)
    repositories.stop(item["id"])
    assert not repositories.get(item["id"])["enabled"]
    assert not alive(item["pid"], item["birth"])
    repositories.recover()
    assert not repositories.get(item["id"])["service_alive"]
    fresh = repositories.start(item["id"])
    assert fresh["pid"] != item["pid"] and repositories.health(fresh)["boot_id"] != old_health["boot_id"]


def test_first_run_auto_installs_repo_resources(config, repositories, resource_repos):
    service = Service(config)
    job = service.submit(str(resource_repos["finance"]))
    final = service.wait(job["id"], 25)
    assert final["status"] == "succeeded", final
    item = repositories.get(final["spec"]["installation_id"])
    assert item["status"] == "ready" and repositories.health(item)
    assert (Path(item["root"]) / "source/installation-count.txt").read_text() == "1"


def test_repo_api_lists_resources(config, repositories, resource_repos):
    from fastapi.testclient import TestClient
    from agent_base.web import create_app
    item = repositories.install(str(resource_repos["finance"]))
    with TestClient(create_app(config, background=False)) as client:
        listing = client.get("/api/repos").json()
        assert len(listing) == 1 and listing[0]["namespace"] == item["namespace"]
        details = client.get("/api/repos/" + item["id"]).json()
        assert details["health"]["skills"] == ["analysis"]
        assert set(details["health"]["tools"]) == {"domain__analyze", "write_report", "read_skill"}


def test_cron_work_binds_existing_install_at_activation(config, repositories, resource_repos):
    from agent_base.scheduler import Scheduler
    scheduler = Scheduler(config)
    scheduler.register("resources", str(resource_repos["finance"]))
    item = repositories.install(str(resource_repos["finance"]))
    trigger = scheduler.trigger("resources")
    job = scheduler.store.get(trigger["run_id"])
    assert job["spec"]["installation_id"] == item["id"]
    scheduler.service.cancel(job["id"])


def test_tool_timeout_does_not_restart_shared_service(config, repositories, resource_repos):
    repo = resource_repos["finance"]
    file = repo / "mcp/domain_server.py"
    file.write_text(file.read_text().replace('    values = arguments["values"]', '    values = arguments["values"]\n    if values == [-1]:\n        await asyncio.sleep(5)'))
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "slow tool"], check=True)
    item = repositories.install(str(repo))
    original = repositories.health(item)
    job = repositories.store.create({"installation_id": item["id"]})
    repositories.store.update(job["id"], status="running", deadline=time.time() + 1)
    async def exercise():
        async with httpx.AsyncClient(headers=repositories.headers(item, job["id"]), trust_env=False) as client:
            async with streamable_http_client(item["endpoint"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool("domain__analyze", {"values": [-1]})
                    assert result.isError and "timeout_outcome_unknown" in str(result.content)
    asyncio.run(exercise())
    repositories.store.update(job["id"], status="timed_out")
    assert repositories.health(repositories.get(item["id"]))["boot_id"] == original["boot_id"]


def test_service_recovery_stops_after_three_failures(repositories, resource_repos, monkeypatch):
    item = repositories.install(str(resource_repos["finance"]))
    calls = []
    monkeypatch.setattr(repositories, "health", lambda _: None)
    def fail(installation_id):
        calls.append(installation_id)
        raise ValueError("fixture unavailable")
    monkeypatch.setattr(repositories, "ensure_service", fail)
    for _ in range(5):
        repositories.update(item["id"], next_retry=0)
        repositories.recover()
    assert len(calls) == 3
    assert repositories.get(item["id"])["restart_attempts"] == 3
