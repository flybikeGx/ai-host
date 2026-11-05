import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_base.admin import Admin
from agent_base.engines import dsh_patch, parse_dsh_output
from agent_base.package import freeze, load_package, within


def test_output_contracts(tmp_path):
    file = tmp_path / "events.jsonl"
    file.write_text('{"type":"session","sessionId":"s"}\n{"type":"final","text":"ok"}\n')
    assert parse_dsh_output(file)["event_contract"] == "legacy_final_exit_code"
    file.write_text('{"type":"session","sessionId":"s"}\n{"type":"final","sessionId":"s","reason":{"kind":"completed"},"text":"ok"}\n')
    assert parse_dsh_output(file)["finish_reason"] == {"kind": "completed"}
    for text in ('garbage', '[]', '{"type":"session","sessionId":"s"}',
                 '{"type":"session","sessionId":"s"}\n{"type":"final","reason":{"kind":"error"}}',
                 '{"type":"session","sessionId":"s"}\n{"type":"error"}\n{"type":"final","text":"fake"}'):
        file.write_text(text)
        with pytest.raises(RuntimeError):
            parse_dsh_output(file)


def test_paths_and_manifest_fail_closed(tmp_path):
    (tmp_path / "AGENTS.md").write_text("hello")
    for name in ("../outside", "/absolute"):
        with pytest.raises(ValueError):
            within(tmp_path, name, exists=False)
    (tmp_path / "link").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        within(tmp_path, "link/outside", exists=False)
    for text in ("schema_version: 9", "schema_version: 1\nunknown: true", "schema_version: 1\ntools:\n  local:\n  - name: bad\n    argv: python hack.py"):
        (tmp_path / "agent.yaml").write_text(text)
        with pytest.raises(ValueError):
            load_package(tmp_path)


def test_patch_does_not_persist_secrets(config, tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-secret-never-materialize")
    patch = dsh_patch(config.runtime, tmp_path, admin_config=config.path)
    text = patch.read_text()
    assert "fixture-secret-never-materialize" not in text
    assert 'process.env["DEEPSEEK_API_KEY"]' in text
    assert "PYTHONPATH" in text


def test_admin_native_session_is_reused(config):
    admin = Admin(config)
    assert admin.turn("列出任务") == "fixture completed"
    native = admin.store.chat_id("default")
    assert native
    assert admin.turn("再查看一次") == "fixture completed"
    captured = json.loads((admin.workspace / "captured-input.json").read_text())
    assert "--session-id" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--session-id") + 1] == native


def test_real_mcp_admin_transport_can_install_and_start(config, repo):
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_base.mcp_server", "--config", str(config.path)],
                                      env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"submit_run", "install_repo", "start_run", "cancel_run"} <= names
                answer = await session.call_tool("install_repo", {"repo": str(repo)})
                assert not answer.isError, answer
                job = json.loads(answer.content[0].text)
                for _ in range(5):
                    answer = await session.call_tool("wait_run", {"run_id": job["id"], "seconds": 5})
                    job = json.loads(answer.content[0].text)
                    if job["status"] == "prepared":
                        break
                assert job["status"] == "prepared", job
                answer = await session.call_tool("start_run", {"run_id": job["id"], "task": "write report"})
                assert not answer.isError, answer
                answer = await session.call_tool("wait_run", {"run_id": job["id"], "seconds": 15})
                assert json.loads(answer.content[0].text)["status"] == "succeeded", answer
    asyncio.run(exercise())


def test_real_mcp_business_tool(config, tmp_path):
    package, workspace = tmp_path / "package", tmp_path / "workspace"
    package.mkdir()
    workspace.mkdir()
    (package / "echo.py").write_text('import sys,json\nx=json.load(sys.stdin)\nprint(json.dumps({"echo":x["arguments"]["text"]}))')
    manifest = {"tools": {"local": [{"name": "echo", "argv": ["${runtime.python}", "${package.root}/echo.py"],
                                     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}}
    (package / "resolved.json").write_text(json.dumps(manifest))
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "agent_base.mcp_server", "--package", str(package), "--workspace", str(workspace)],
                                      env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                answer = await session.call_tool("echo", {"text": "中文工具"})
                assert not answer.isError, answer
                assert json.loads(answer.content[0].text) == {"echo": "中文工具"}
                bad = await session.call_tool("echo", {})
                assert bad.isError
    asyncio.run(exercise())
