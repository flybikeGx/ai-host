"""A deterministic CLI fixture, never calls an external model."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import yaml

if "--version" in sys.argv:
    print("0.2.1-rc.5")
    sys.exit(0)
text = sys.stdin.read()
patch = yaml.load(Path(sys.argv[sys.argv.index("--patch") + 1]).read_text(), Loader=yaml.BaseLoader)
selection = next(item["config"] for item in patch if item.get("id") == "agent-default-model")
session = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else "session-" + uuid.uuid4().hex
print(json.dumps({"type": "session", "sessionId": session}), flush=True)
Path("captured-input.json").write_text(json.dumps({"prompt": text, "argv": sys.argv[1:], "selection": selection,
                                                   "home": os.environ.get("DSH_HOME")}, ensure_ascii=False))
if "FIXTURE_SLEEP" in text:
    time.sleep(10)
if "FIXTURE_STREAM" in text:
    print(json.dumps({"type": "text", "text": "正在生成测试回复…"}, ensure_ascii=False), flush=True)
    time.sleep(3)
if "FIXTURE_FAIL" in text:
    print("fixture runtime failure", file=sys.stderr)
    sys.exit(9)
if Path(".agent/resources.json").is_file() and "FIXTURE_NO_ARTIFACT" not in text:
    # Deterministic model stand-in, using the real persistent MCP protocol.
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    config = next(row["insert"][0]["config"] for row in patch if "insert" in row)
    async def exercise():
        headers = {"Authorization": os.environ["AGENT_BASE_REPO_AUTH"], "X-Agent-Run": config["headers"]["X-Agent-Run"]}
        async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
            async with streamable_http_client(config["url"], http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as connection:
                    await connection.initialize()
                    async def invoke(name, arguments):
                        call_id = uuid.uuid4().hex
                        print(json.dumps({"type": "tool_call", "callId": call_id, "tool": "mcp__" + config["serverName"] + "__" + name, "input": arguments}), flush=True)
                        answer = await connection.call_tool(name, arguments)
                        print(json.dumps({"type": "tool_result", "callId": call_id, "status": "failed" if answer.isError else "completed", "result": answer.content[0].text}), flush=True)
                        return answer
                    names = [tool.name for tool in (await connection.list_tools()).tools]
                    skill = await invoke("read_skill", {"name": "analysis"})
                    assert not skill.isError, skill
                    answer = await invoke("domain__analyze", {"values": [2, 3, 5]})
                    assert not answer.isError, answer
                    analysis = json.loads(answer.content[0].text)
                    result = await invoke("write_report", {"analysis": analysis,
                        "skill_marker": analysis["domain"].upper() + "_SKILL_OK"})
                    assert not result.isError, result
                    Path("captured-tools.json").write_text(json.dumps({"names": names, "skill": json.loads(skill.content[0].text)}))
    asyncio.run(exercise())
elif "FIXTURE_NO_ARTIFACT" not in text:
    Path("artifacts").mkdir(exist_ok=True)
    Path("artifacts/report.txt").write_text("fixture report\n" + text, encoding="utf-8")
answer = "fixture completed"
if "FIXTURE_MARKDOWN" in text:
    answer = "## 测试回复\n\n- **中文内容**\n- `relative/path`\n\n```python\nprint('hello')\n```\n\n<script>window.__xss=true</script>\n[unsafe](javascript:alert(1))"
if "<agent_base_report_contract>" in text:
    if "REPORT_ONLY_EXTRA" in text:
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/extra.txt").write_text("extra output")
    report = {"schema_version": 1, "status": "completed", "summary": "fixture completed",
              "artifacts": [{"path": p.as_posix()} for p in sorted(Path("artifacts").glob("*")) if p.is_file()],
              "checks": [], "issues": [], "next_steps": []}
    if "REPORT_BLOCKED" in text:
        report.update(status="blocked", issues=["需要业务输入"], next_steps=["补充 input 数据"])
    if "REPORT_MISSING" in text:
        report["artifacts"].append({"path": "artifacts/missing.json"})
    if "REPORT_FAILED_CHECK" in text:
        report["checks"] = [{"name": "agent self check", "status": "failed"}]
    if "REPORT_SELF_CHECK" in text:
        report["checks"] = [{"name": "agent self check", "status": "passed"}]
    answer = "not JSON" if "REPORT_INVALID" in text else json.dumps(report, ensure_ascii=False)
print(json.dumps({"type": "final", "text": answer}, ensure_ascii=False))
