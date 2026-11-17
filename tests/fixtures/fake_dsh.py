"""A deterministic CLI fixture, never calls an external model."""
import json
import os
import re
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
if 'FIXTURE_KNOWLEDGE' in text:
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    knowledge_config=next(plugin['config'] for row in patch for plugin in row.get('insert',[]) if plugin['id']=='knowledge')
    async def exercise_knowledge():
        async with httpx.AsyncClient(headers={'Authorization':os.environ['AGENT_BASE_KNOWLEDGE_AUTH']},trust_env=False) as client:
            async with streamable_http_client(knowledge_config['url'],http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize()
                    async def invoke(name,arguments):
                        call_id=uuid.uuid4().hex
                        print(json.dumps({'type':'tool_call','callId':call_id,'tool':'mcp__knowledge__'+name,'input':arguments}),flush=True)
                        result=await session.call_tool(name,arguments)
                        print(json.dumps({'type':'tool_result','callId':call_id,'status':'failed' if result.isError else 'completed','result':result.content[0].text}),flush=True)
                        assert not result.isError,result
                        return json.loads(result.content[0].text)
                    catalog=await invoke('knowledge_catalog',{})
                    namespace=catalog['namespaces'][0]['id']
                    results=await invoke('knowledge_search',{'namespace':namespace,'query':'共享知识'})
                    if 'FIXTURE_KNOWLEDGE_SUBMIT' in text:
                        await invoke('knowledge_submit',{'namespace':namespace,'operation':'create','candidate':{'title':'共享知识贡献','summary':'经过审批后共享','body_markdown':'# 经验\n共享知识来自完整运行链路。'},'reason':'验证跨实例复用','idempotency_key':'fixture-'+session.__class__.__name__})
                    if results['items']:
                        doc=results['items'][0]
                        outline=await invoke('knowledge_read',{'document_id':doc['id'],'version_id':doc['version_id'],'view':'outline'})
                        await invoke('knowledge_read',{'document_id':doc['id'],'version_id':doc['version_id'],'view':'section','section_id':outline['sections'][0]['id']})
                    Path('captured-knowledge.json').write_text(json.dumps({'hits':len(results['items']),'namespace':namespace,'control_secret_in_environment':'AGENT_BASE_WEB_TOKEN' in os.environ}))
    asyncio.run(exercise_knowledge())
if 'FIXTURE_SERVICES' in text:
    # Deterministic model stand-in exercising every mounted shared service via
    # the real MCP chain (resolver -> gateway -> backend).
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import McpError
    services = [plugin['config'] for row in patch for plugin in row.get('insert', [])
                if str(plugin.get('id', '')).startswith('shared-service-')]

    def resolve_header(value):
        # BaseLoader keeps process.env["NAME"] expressions as plain strings.
        match = re.fullmatch(r'process\.env\["([^"]+)"\]', str(value))
        return os.environ[match.group(1)] if match else str(value)

    async def exercise_services():
        results = []
        for config in services:
            alias = config['serverName']
            headers = {name: resolve_header(value) for name, value in (config.get('headers') or {}).items()}
            async with httpx.AsyncClient(headers=headers, trust_env=False) as client:
                async with streamable_http_client(config['url'], http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = [t.name for t in (await session.list_tools()).tools]
                        entry = {'alias': alias, 'url': config['url'], 'tools': tools, 'calls': []}
                        for tool, arguments in ([('ticket_search', {'query': '支付'})]
                                                + ([('ticket_subscribe', {'agent': 'fixture-agent', 'project': 'demo'})]
                                                   if 'ticket_subscribe' in tools else [])):
                            call_id = uuid.uuid4().hex
                            print(json.dumps({'type': 'tool_call', 'callId': call_id,
                                              'tool': f'mcp__{alias}__{tool}', 'input': arguments}), flush=True)
                            try:
                                answer = await session.call_tool(tool, arguments)
                                print(json.dumps({'type': 'tool_result', 'callId': call_id,
                                                  'status': 'failed' if answer.isError else 'completed',
                                                  'result': answer.content[0].text if answer.content else ''}), flush=True)
                                entry['calls'].append({'tool': tool, 'ok': not answer.isError,
                                                       'result': json.loads(answer.content[0].text) if answer.content and not answer.isError else None})
                            except McpError as error:
                                print(json.dumps({'type': 'tool_result', 'callId': call_id, 'status': 'failed',
                                                  'result': str(error)}), flush=True)
                                entry['calls'].append({'tool': tool, 'ok': False, 'error': str(error)})
                        results.append(entry)
        Path('artifacts').mkdir(exist_ok=True)
        Path('artifacts/services.json').write_text(json.dumps({'services': results}, ensure_ascii=False, indent=2))
        Path('captured-services.json').write_text(json.dumps({'count': len(results),
                                                               'aliases': [r['alias'] for r in results]}))
    asyncio.run(exercise_services())
if 'FIXTURE_GUARDIAN' in text:
    import asyncio
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters
    config = next(plugin['config'] for row in patch for plugin in row.get('insert', [])
                  if plugin.get('id') == 'guardian-tools')
    focus = None
    for token in text.split():
        if token.startswith('FIXTURE_GUARDIAN_DEPLOYMENT:'):
            focus = token.split(':', 1)[1]
    async def exercise_guardian():
        async with stdio_client(StdioServerParameters(command=config['command'], args=config['args'],
                                                      env={**os.environ, **config.get('env', {})})) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                async def invoke(name, arguments):
                    call_id = uuid.uuid4().hex
                    print(json.dumps({'type': 'tool_call', 'callId': call_id, 'tool': 'mcp__guardian__' + name,
                                      'input': arguments}), flush=True)
                    answer = await session.call_tool(name, arguments)
                    payload = json.loads(answer.content[0].text) if answer.content else {}
                    print(json.dumps({'type': 'tool_result', 'callId': call_id,
                                      'status': 'failed' if answer.isError else 'completed'}), flush=True)
                    assert not answer.isError, answer
                    return payload
                overview = await invoke('services_overview', {})
                actions = {'overview_deployments': len(overview['deployments']),
                           'supervisor_state': overview['supervisor']['state']}
                if focus:
                    await invoke('service_detail', {'deployment_id': focus})
                    await invoke('read_service_log', {'deployment_id': focus, 'name': 'gateway.log'})
                    probe = await invoke('probe_service', {'deployment_id': focus})
                    actions['probe_state'] = probe.get('state')
                    incidents = await invoke('list_incidents', {'deployment_id': focus})
                    for incident in incidents:
                        await invoke('record_incident_note', {'incident_id': incident['id'],
                                                              'note': 'fixture guardian diagnosis: probe recorded'})
                    if 'FIXTURE_GUARDIAN_RESTART' in text:
                        # The restricted tool still enforces restart_safe, the
                        # deployment authorization, the recovery budget and drain.
                        try:
                            restarted = await invoke('restart_service', {
                                'deployment_id': focus, 'reason': 'fixture guardian repair'})
                            actions['restart'] = {'ok': True,
                                                  'generation': restarted.get('generation')}
                        except BaseException as error:
                            actions['restart'] = {'ok': False, 'error': repr(error)[:200]}
                Path('captured-guardian.json').write_text(json.dumps(actions, ensure_ascii=False))
    asyncio.run(exercise_guardian())
if Path(".agent/resources.json").is_file() and "FIXTURE_NO_ARTIFACT" not in text and "FIXTURE_SERVICES" not in text:
    # Deterministic model stand-in, using the real persistent MCP protocol.
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    config = next(plugin['config'] for row in patch for plugin in row.get('insert', [])
                  if plugin.get('id') == 'repo-resources')
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
