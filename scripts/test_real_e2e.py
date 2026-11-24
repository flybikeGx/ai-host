"""Real-process end-to-end check for the AIHO-32 information board.

The AIHO-33 D1 baseline does not contain gitee/main's multiuser chat e2e
fixtures (identity, migration seeds, `playwright.real.config.js`), so this
branch's `scripts/ci.py full` real-process stage verifies the capability this
task adds instead: a real `agent-base serve` process, CLI install/enable/disable,
a real Streamable HTTP MCP call, the human read API, and persisted data across
a disable/enable cycle. It uses the deterministic fake CLI fixture only for the
optional run-path check; no model credentials are read.

Exit 0 on success; non-zero on any assertion.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(os.environ.get("CI_REPORT", ROOT / "frontend/test-results/real-processes"))
REPORT.mkdir(parents=True, exist_ok=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def env_for(extra: dict) -> dict:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "CI_REPORT")
           if key in os.environ}
    env["CI_REPORT"] = str(REPORT)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
    env.update(extra)
    return env


def wait(url: str, child: subprocess.Popen, seconds: float = 30) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if child.poll() is not None:
            raise AssertionError(f"server exited early: {child.returncode}")
        try:
            with urlopen(url, timeout=0.3) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server did not become ready: {url}")


def cli(config: Path, *arguments: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "agent_base", "--config", str(config), *arguments],
                            cwd=ROOT, env=env_for({}), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(f"agent-base {' '.join(arguments)} failed: {result.stderr}")
    return json.loads(result.stdout)


def get_json(url: str) -> tuple[int, dict]:
    with urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read() or b"{}")


def http_status(url: str) -> int:
    try:
        with urlopen(url, timeout=5) as response:
            return response.status
    except OSError as error:
        status = getattr(error, "code", None)
        if status is None:
            raise
        return status


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-base-info-e2e-") as directory:
        base = Path(directory)
        config = base / "fixture.toml"
        config.write_text(
            '[storage]\nroot="data"\n[queue]\nmax_concurrent=1\n[runtime]\nengine="dsh"\ncommand='
            + json.dumps([sys.executable, str(ROOT / "tests/fixtures/fake_dsh.py")])
            + '\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=20\n', encoding="utf-8")
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        log = (REPORT / "info-board-server.log").open("w")
        child = subprocess.Popen([sys.executable, "-m", "agent_base", "--config", str(config),
                                  "serve", "--port", str(port)],
                                 cwd=ROOT, env=env_for({}), stdout=log, stderr=log)
        checks = {}
        try:
            wait(url + "/api/auth/session", child)
            checks["page_absent_before_install"] = http_status(url + "/info") == 404

            cli(config, "mcp", "install", "info-board")
            status, packages = get_json(url + "/api/mcp/packages")
            checks["navigation_visible"] = status == 200 and [item["id"] for item in packages] == ["info-board"]
            checks["page_served"] = http_status(url + "/info") == 200
            checks["api_empty"] = get_json(url + "/api/info/posts")[1]["items"] == []

            from agent_base.mcp_runtime import issue_grant, verify_grant
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
            import httpx
            root = config.parent / "data/info-board"
            token = issue_grant((root / "service-token").read_text(encoding="utf-8").strip(),
                                actor="repo:e2e", expires=time.time() + 300)
            assert verify_grant((root / "service-token").read_text(encoding="utf-8").strip(), token)

            async def create_post():
                async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                             trust_env=False) as client:
                    async with streamable_http_client(
                            (json.loads((root / "service.json").read_text()))["endpoint"],
                            http_client=client) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            result = await session.call_tool("info_create", {
                                "title": "端到端信息", "body_markdown": "# 端到端\n真实 MCP 链路。",
                                "tags": ["e2e"]})
                            assert not result.isError, result
                            return json.loads(result.content[0].text)

            created = asyncio.run(create_post())
            checks["mcp_created"] = created["author"] == "repo:e2e"
            body = get_json(url + "/api/info/posts")[1]
            checks["list_summary"] = body["items"][0]["id"] == created["id"] and "body_markdown" not in body["items"][0]
            checks["detail_body"] = get_json(url + f"/api/info/posts/{created['id']}")[1]["body_markdown"].startswith("# 端到端")

            cli(config, "mcp", "disable", "info-board")
            checks["disabled_page_404"] = http_status(url + "/info") == 404
            checks["disabled_api_404"] = http_status(url + "/api/info/posts") == 404
            checks["disabled_navigation"] = get_json(url + "/api/mcp/packages")[1] == []

            cli(config, "mcp", "enable", "info-board")
            checks["enabled_again"] = get_json(url + "/api/info/posts")[1]["items"][0]["id"] == created["id"]
            checks["data_persisted"] = http_status(url + "/info") == 200

            cli(config, "mcp", "uninstall", "info-board")
            checks["uninstalled_404"] = http_status(url + "/info") == 404
        except BaseException:
            (REPORT / "summary.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
            raise
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            log.close()
            try:
                from agent_base.config import Config
                from agent_base.mcp_runtime import PackageRuntime, package
                loaded = Config.load(config)
                PackageRuntime(loaded, package(loaded, "info-board")).stop()
            except Exception:
                pass
        assert all(checks.values()), checks
        (REPORT / "summary.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        print(json.dumps(checks, ensure_ascii=False), flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
