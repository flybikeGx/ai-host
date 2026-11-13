"""Real-dsh provider validation against a local OpenAI-compatible test gateway.

Proves the configured provider route actually carries model selection, a tool
call and the multi-turn tool result back through dsh — not just patch text.
Skipped when the local .runtime/dsh installation is unavailable.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_base.config import Config, PROJECT
from agent_base.service import Service

DSH_BIN = PROJECT / ".runtime" / "dsh" / "node_modules" / ".bin" / ("dsh.cmd" if os.name == "nt" else "dsh")

requires_local_dsh = pytest.mark.skipif(not DSH_BIN.is_file(), reason="本地 .runtime/dsh 不可用")

TOOL_CALL_ARGUMENTS = {"text": "gateway-roundtrip"}


class FakeGateway:
    """OpenAI-compatible gateway: round 1 issues a tool call, round 2 answers."""

    def __init__(self):
        self.requests = []
        self.authorizations = []
        self.models = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                gateway.requests.append(body)
                gateway.authorizations.append(self.headers.get("Authorization"))
                gateway.models.append(body.get("model"))
                try:
                    if not any(message.get("role") == "tool" for message in body.get("messages", [])):
                        payload = gateway.tool_call_response(body)
                    else:
                        payload = gateway.final_response(body)
                except Exception as error:  # surface scripting bugs in run logs
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(error).encode())
                    return
                stream = body.get("stream") is not False
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for chunk in payload:
                        self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    content = json.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def chunk(model, delta, finish=None):
        return {"id": "chatcmpl-gateway", "object": "chat.completion.chunk", "created": 1, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def tool_call_response(self, body):
        tool = next(entry for entry in body.get("tools", [])
                    if entry.get("function", {}).get("name", "").endswith("gateway_report"))
        model = body.get("model", "internal-flash")
        return [
            self.chunk(model, {"role": "assistant", "content": ""}),
            self.chunk(model, {"tool_calls": [{"index": 0, "id": "call-gateway-1", "type": "function",
                                               "function": {"name": tool["function"]["name"],
                                                            "arguments": json.dumps(TOOL_CALL_ARGUMENTS)}}]}),
            self.chunk(model, {}, finish="tool_calls"),
        ]

    def final_response(self, body):
        model = body.get("model", "internal-flash")
        report = {"schema_version": 1, "status": "completed", "summary": "gateway roundtrip completed",
                  "artifacts": [{"path": "artifacts/gateway-report.txt"}],
                  "checks": [{"name": "gateway tool executed", "status": "passed"}],
                  "issues": [], "next_steps": []}
        return [
            self.chunk(model, {"role": "assistant", "content": json.dumps(report, ensure_ascii=False)}),
            self.chunk(model, {}, finish="stop"),
        ]


@pytest.fixture
def gateway():
    server = FakeGateway()
    yield server
    server.stop()


def gateway_repo(repo: Path, credential_env: str):
    repo.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("按任务调用 gateway_report 工具并交付 artifacts/gateway-report.txt。", encoding="utf-8")
    (repo / "tool_report.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "request = json.load(sys.stdin)\n"
        "text = request['arguments']['text']\n"
        "Path('artifacts').mkdir(exist_ok=True)\n"
        "Path('artifacts/gateway-report.txt').write_text('gateway tool output: ' + text, encoding='utf-8')\n"
        "print(json.dumps({'ok': True, 'received': text,"
        f" 'provider_credential_visible': {credential_env!r} in os.environ}}))\n", encoding="utf-8")
    manifest = {"schema_version": 1,
                "task": {"default": "调用 gateway_report 工具，text 参数 gateway-roundtrip，然后交付报告。"},
                "tools": {"local": [{"name": "gateway_report", "argv": ["${runtime.python}", "${package.root}/tool_report.py"],
                                     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                                                      "required": ["text"], "additionalProperties": False}}]},
                "completion": {"artifacts": [{"path": "artifacts/gateway-report.txt", "min_bytes": 1}]}}
    (repo / "agent.yaml").write_text(__import__("yaml").safe_dump(manifest, allow_unicode=True), encoding="utf-8")
    for command in (["git", "init", "--quiet", str(repo)],
                    ["git", "-C", str(repo), "add", "."],
                    ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "core.hooksPath=" + os.devnull, "commit", "--quiet", "-m", "gateway fixture"]):
        subprocess.run(command, check=True)
    return repo


@requires_local_dsh
@pytest.mark.parametrize("credential_env", ["FAKE_GATEWAY_API_KEY", "ACCEPTANCE_LOGIN"],
                         ids=["keyword-name", "neutral-name"])
def test_custom_provider_runs_real_dsh_through_local_gateway(tmp_path, gateway, monkeypatch, credential_env):
    # A neutral name (no KEY/TOKEN/SECRET/PASSWORD) is the regression that the
    # keyword-only filters missed: isolation must follow the declared name.
    monkeypatch.setenv(credential_env, "fake-gateway-secret-do-not-log")
    repo = gateway_repo(tmp_path / "repo", credential_env)
    file = tmp_path / "config.toml"
    file.write_text(
        '[storage]\nroot = "data"\n[queue]\nmax_concurrent = 1\n[runtime]\n'
        f'engine = "dsh"\ncommand = [{json.dumps(str(DSH_BIN))}]\n'
        'provider = "internal-gateway"\nmodel = "internal-flash"\neffort = "high"\n'
        'timeout_seconds = 240\npermission_mode = "workspace-write"\napproval_policy = "never"\n\n'
        f'[providers.internal-gateway]\napi = "openai-completions"\nbase_url = "{gateway.url}"\n'
        f'api_key_env = "{credential_env}"\n\n'
        '[[providers.internal-gateway.models]]\nid = "internal-flash"\ncontext_window = 131072\n'
        '[providers.internal-gateway.models.reasoning_efforts]\nhigh = "high"\n', encoding="utf-8")
    configuration = Config.load(file)
    service = Service(configuration)
    final = service.wait(service.submit(str(repo))["id"], 200)
    assert final["status"] == "succeeded", final
    # Model selection: every model request names the configured catalog id.
    assert gateway.models and all(model == "internal-flash" for model in gateway.models)
    assert all(value == "Bearer fake-gateway-secret-do-not-log" for value in gateway.authorizations)
    # Tool call and multi-turn tool result round trip.
    first = gateway.requests[0]
    assert any(entry.get("function", {}).get("name", "").endswith("gateway_report") for entry in first.get("tools", []))
    continuation = next(request for request in gateway.requests[1:]
                         if any(message.get("role") == "tool" for message in request.get("messages", [])))
    tool_messages = [message for message in continuation["messages"] if message.get("role") == "tool"]
    assert any("gateway-roundtrip" in json.dumps(message, ensure_ascii=False) for message in tool_messages)
    # The business tool never sees the declared provider credential, whatever
    # its name; its own JSON output is embedded as a content string.
    assert any('"provider_credential_visible": false' in str(message.get("content")) for message in tool_messages)
    # Structured report and archived artifact prove the loop really completed.
    assert final["result"]["agent_report"]["status"] == "completed"
    artifact = Path(final["result"]["artifacts"][0]["path"]).read_text(encoding="utf-8")
    assert artifact == "gateway tool output: gateway-roundtrip"
    # No credential value leaks into run records or logs.
    root = Path(final["root"])
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_size < 2 * 1024 * 1024:
            assert "fake-gateway-secret-do-not-log" not in path.read_text(encoding="utf-8", errors="replace"), path
    # The invocation patch configures the dormant pi-ai adapter in place and
    # routes through the custom provider, not the official one.
    patch = (root / "runtime.patch.yml").read_text(encoding="utf-8")
    assert "llm-pi-ai" in patch and "internal-gateway" in patch
    assert "fake-gateway-secret-do-not-log" not in patch
    # Only environment variable names may appear; the alias carries the value.
    assert "apiKeyEnv: " + "AGENT_BASE_PROVIDER_API_KEY" in patch
