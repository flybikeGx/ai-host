import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_base.knowledge import Knowledge,KnowledgeError
from agent_base.knowledge_mcp import call,TOOLS
from agent_base.knowledge_service import KnowledgeService,attach
from agent_base.web import create_app


def content(title='日切规则',body='# 日切时区\n每日北京时间零点结算。\n# 跨日补单\n补单进入下一结算周期。'):
    return {'title':title,'summary':'结算与补单的项目规则','body_markdown':body,'tags':['结算'],'topic_path':'结算','slug':''}


@pytest.fixture
def kb(config):return Knowledge(config)


def project(kb,slug='project-a'):
    return kb.save_namespace({'slug':slug,'name':slug,'description':'项目规则'})['id']


def grant(kb,ns,actor='admin'):
    binding=kb.binding(actor)
    if not binding:binding=kb.bind(actor,{'primary_namespace_id':ns,'read_namespace_ids':[ns],'submit_namespace_ids':[ns]})
    return binding


def test_publication_submission_isolation_and_versions(kb):
    a,b=project(kb),project(kb,'project-b');g=grant(kb,a)
    submission=kb.submit(g,a,'create',content(),'来源验证','new-a')
    assert kb.list_documents(a)['items']==[]
    with pytest.raises(KnowledgeError):kb.submission(submission['id'],{**g,'actor':'other'})
    assert kb.submit(g,a,'create',content(),'来源验证','new-a')['id']==submission['id']
    with pytest.raises(KnowledgeError) as error:kb.submit(g,a,'create',content('另一个'),'来源验证','new-a')
    assert error.value.status==409
    approved=kb.review(submission['id'],'approve',submission['content_hash'],None)
    doc=kb.document(approved['document_id']);assert doc['version_number']==1
    with pytest.raises(KnowledgeError):kb.review(submission['id'],'approve',submission['content_hash'],None)
    assert kb.list_documents(a,'北京时间',grant=g)['items'][0]['id']==doc['id']
    hidden=kb.publish(b,content('其他项目'))
    with pytest.raises(KnowledgeError):kb.document(hidden['id'],grant=g)
    updated=kb.publish(a,content(body='# 新规则\n北京时间凌晨一点。'),doc['id'],doc['version_id'])
    assert updated['version_number']==2 and kb.document(doc['id'],doc['version_id'])['candidate']==doc['candidate']
    assert not kb.list_documents(a,'每日北京时间零点',grant=g)['items']
    kb.archive(doc['id'],updated['version_id'])
    assert not kb.list_documents(a,grant=g)['items']
    assert kb.document(doc['id'],doc['version_id'],g)['status']=='archived'
    restored=kb.publish(a,doc['candidate'],doc['id'],updated['version_id']);assert restored['version_number']==3


def test_concurrent_proposals_conflict_and_amended_publication(kb):
    ns=project(kb);g=grant(kb,ns);doc=kb.publish(ns,content())
    first=kb.submit(g,ns,'update',content('修改 A'),'A','a',doc['id'],doc['version_id'])
    second=kb.submit(g,ns,'update',content('修改 B'),'B','b',doc['id'],doc['version_id'])
    kb.review(first['id'],'approve',first['content_hash'],doc['version_id'])
    assert kb.submission(second['id'])['stale']
    with pytest.raises(KnowledgeError) as error:kb.review(second['id'],'approve',second['content_hash'],doc['version_id'])
    assert error.value.status==409 and kb.submission(second['id'])['status']=='pending'
    current=kb.document(doc['id'])
    final=kb.review(second['id'],'amend',second['content_hash'],current['version_id'],'合并 A 与 B',content('合并版'))
    assert final['status']=='superseded' and final['candidate']['title']=='修改 B'
    assert kb.document(doc['id'])['candidate']['title']=='合并版'
    assert len(kb.history(doc['id']))==3


def test_atomic_publish_rollback_and_rejection(kb,monkeypatch):
    ns=project(kb);g=grant(kb,ns);proposal=kb.submit(g,ns,'create',content(),'待核实','x')
    original=kb.audit
    def fail(c,action,*args,**kwargs):
        if action=='document.publish':raise RuntimeError('simulated disk failure')
        return original(c,action,*args,**kwargs)
    monkeypatch.setattr(kb,'audit',fail)
    with pytest.raises(RuntimeError):kb.review(proposal['id'],'approve',proposal['content_hash'],None)
    assert not kb.list_documents(ns)['items'] and kb.submission(proposal['id'])['status']=='pending'
    monkeypatch.setattr(kb,'audit',original)
    with pytest.raises(KnowledgeError):kb.review(proposal['id'],'reject',proposal['content_hash'],None)
    assert kb.review(proposal['id'],'reject',proposal['content_hash'],None,'依据不充分')['status']=='rejected'


def test_progressive_disclosure_cursor_version_and_response_budget(kb):
    ns=project(kb);g=grant(kb,ns);body='# 很长的规则\n'+('中文内容\\\n'*4000)+'\n# 第二节\n完。'
    doc=kb.publish(ns,content(body=body))
    search=call(kb,g,'knowledge_search',{'namespace':ns,'query':'中文内容'})
    assert 'body_markdown' not in json.dumps(search) and len(json.dumps(search,ensure_ascii=False))<8000
    outline=call(kb,g,'knowledge_read',{'document_id':doc['id'],'version_id':doc['version_id'],'view':'outline'})
    assert len(outline['sections'])==2
    args={'document_id':doc['id'],'version_id':doc['version_id'],'view':'section','section_id':'s0','max_chars':6000}
    result=call(kb,g,'knowledge_read',args);parts=[result['markdown']]
    kb.publish(ns,content('新版本'),doc['id'],doc['version_id'])
    while result['next_cursor']:
        result=call(kb,g,'knowledge_read',{**args,'cursor':result['next_cursor']});parts.append(result['markdown'])
        assert len(json.dumps(result,ensure_ascii=False))<=8000 and not result['is_current']
    assert ''.join(parts)==body[:doc['sections'][0]['end']]
    with pytest.raises(KnowledgeError):call(kb,g,'knowledge_read',{**args,'section_id':'s1','cursor':call(kb,g,'knowledge_read',args)['next_cursor']})
    assert not any('approve' in tool['name'] or 'publish' in tool['name'] for tool in TOOLS)


def test_grant_expiry_revocation_and_management_secret_not_in_runtime(kb,config,monkeypatch):
    from agent_base.engines import runtime_environment
    ns=project(kb);g=grant(kb,ns);token=kb.issue('agent',g,60)
    assert kb.grant(token)['actor']=='admin'
    kb.bind('admin',{**{k:v for k,v in g.items() if k not in {'actor','revision'}},'expected_revision':g['revision'],'enabled':False})
    with pytest.raises(KnowledgeError):kb.grant(token)
    expired=kb.issue('agent',g,-1)
    with pytest.raises(KnowledgeError):kb.grant(expired)
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN','fixture-control-secret')
    monkeypatch.setenv('AGENT_BASE_KNOWLEDGE_AUTH','fixture-previous-grant')
    env=runtime_environment(config.runtime,config.data/'home')
    assert 'AGENT_BASE_WEB_TOKEN' not in env and 'AGENT_BASE_KNOWLEDGE_AUTH' not in env


def test_http_home_routes_bootstrap_and_admin_controls(config,kb):
    with TestClient(create_app(config,background=False)) as client:
        assert client.get('/',follow_redirects=False).headers['location']=='/dashboard'
        for route in ('/dashboard','/chat','/cron','/knowledge','/workbench'):assert client.get(route).status_code==200
        assert not client.get('/api/knowledge/status').json()['can_write']
        assert client.post('/api/knowledge/namespaces',json={'slug':'x','name':'X'}).status_code==401
        assert client.get('/api/knowledge/submissions?namespace=x').status_code==401
        assert client.get('/api/knowledge/bindings').status_code==401
        ticket=kb.issue('bootstrap',{'actor':'local-admin'},60)
        assert client.post('/api/knowledge/connect',json={'ticket':ticket}).status_code==200
        assert client.post('/api/knowledge/connect',json={'ticket':ticket}).status_code==401
        assert client.get('/api/knowledge/status').json()['can_write']
        ns=client.post('/api/knowledge/namespaces',json={'slug':'x','name':'X'}).json()['id']
        response=client.post('/api/knowledge/documents',json={'namespace':ns,'candidate':content()});assert response.status_code==200,response.text
        doc=response.json()
        assert client.put('/api/knowledge/documents/'+doc['id'],json={'candidate':content('冲突'),'expected_version_id':'wrong'}).status_code==409
        assert client.post('/api/knowledge/namespaces',json={'slug':'evil','name':'evil'},headers={'Origin':'https://elsewhere.invalid'}).status_code==403
        assert client.get('/api/knowledge/namespaces',headers={'Host':'evil.invalid'}).status_code==400
        assert client.get('/api/knowledge/status').json()['service']['state']=='down'


def test_real_central_mcp_scoped_read_and_submit(config,kb):
    import asyncio
    ns=project(kb);g=grant(kb,ns);doc=kb.publish(ns,content())
    service=KnowledgeService(config);first=service.ensure();assert service.ensure()['pid']==first['pid']
    token=kb.issue('agent',g,60)
    async def exercise():
        async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False) as client:
            async with streamable_http_client(first['endpoint'],http_client=client) as streams:
                async with ClientSession(streams[0],streams[1]) as session:
                    await session.initialize();assert len((await session.list_tools()).tools)==6
                    answer=await session.call_tool('knowledge_search',{'namespace':ns,'query':'日切'})
                    assert not answer.isError and json.loads(answer.content[0].text)['items'][0]['id']==doc['id']
                    answer=await session.call_tool('knowledge_submit',{'namespace':ns,'operation':'create','candidate':content('来自 MCP'),'reason':'真实工具投稿','idempotency_key':'mcp-one'})
                    assert not answer.isError and json.loads(answer.content[0].text)['status']=='pending'
    asyncio.run(exercise())
    assert len(kb.list_documents(ns)['items'])==1
    assert service.status()['pid']==first['pid']


def test_bound_runtime_mounts_one_shared_service(config,kb,tmp_path):
    from agent_base.engines import dsh_patch
    import yaml
    ns=project(kb);grant(kb,ns)
    root=tmp_path/'turn';root.mkdir();patch=dsh_patch(config.runtime,root);env={}
    instructions=attach(config,patch,env,'admin',{'turn_id':'fixture'},time.time()+60)
    assert 'knowledge_catalog/search' in instructions and 'AGENT_BASE_KNOWLEDGE_AUTH' in env
    rows=yaml.load(patch.read_text(),Loader=yaml.BaseLoader)
    mounted=[p for row in rows for p in row.get('insert',[]) if p['id']=='knowledge']
    assert len(mounted)==1 and mounted[0]['config']['serverName']=='knowledge'
    assert env['AGENT_BASE_KNOWLEDGE_AUTH'] not in patch.read_text()


def test_three_repos_share_only_approved_project_knowledge(config,kb,repositories,resource_repos):
    from pathlib import Path
    from agent_base.service import Service
    a,b=project(kb),project(kb,'project-b')
    installed=[repositories.install(str(repo)) for repo in resource_repos.values()]
    for item,ns in zip(installed,[a,a,b]):grant(kb,ns,'repo:'+item['repo_id'])
    service=Service(config)
    def execute(item,task):
        job=service.submit(item['origin'],task)
        result=service.wait(job['id'],25)
        assert result['status']=='succeeded',result
        path=Path(result['root'])/'runtime.jsonl'
        events=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        calls=[event for event in events if event.get('type')=='tool_call' and event.get('tool','').startswith('mcp__knowledge__')]
        assert calls
        return calls
    execute(installed[0],'FIXTURE_KNOWLEDGE_SUBMIT')
    proposal=kb.submissions(a)['items'][0];full=kb.submission(proposal['id'])
    assert not kb.list_documents(a)['items'] and full['source']['run_id'] and full['source']['commit']
    kb.review(full['id'],'approve',full['content_hash'],None)
    reader=execute(installed[1],'FIXTURE_KNOWLEDGE')
    assert sum(c['tool'].endswith('knowledge_read') for c in reader)==2
    other=execute(installed[2],'FIXTURE_KNOWLEDGE')
    assert not any(c['tool'].endswith('knowledge_read') for c in other)
