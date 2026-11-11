import copy
import json
import os
import signal
import subprocess
import sys
import time
from collections import namedtuple
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from fastapi.testclient import TestClient

import agent_base.dashboard as module
from agent_base.conversations import Conversations
from agent_base.dashboard import Dashboard,ProcessSampler,pid_state
from agent_base.scheduler import Scheduler
from agent_base.web import create_app


def test_cpu_warmup_history_and_no_execution(config,monkeypatch):
    dashboard=Dashboard(config,Scheduler(config))
    cpu=namedtuple('CPU','user system idle')
    values=iter([cpu(10,0,90),cpu(40,0,160)]+[cpu(40+i,0,160+i) for i in range(130)])
    monkeypatch.setattr(module.psutil,'cpu_times',lambda:next(values))
    monkeypatch.setattr(module.ProcessSampler,'collect',lambda *args:({}, {'cpu_percent':None,'rss_bytes':None,'process_count':0,'cpu_complete':False,'rss_complete':False,'unreadable':0},{}))
    def forbidden(*args,**kwargs):raise AssertionError('dashboard must not launch or recover resources')
    monkeypatch.setattr('agent_base.service.Service.submit',forbidden)
    monkeypatch.setattr('agent_base.repositories.Repositories.ensure_service',forbidden)
    dashboard.sample()
    first=dashboard.snapshot()
    assert first['available'] and first['host']['cpu_percent'] is None
    assert first['counts']['running_instances']==0
    assert first['scheduler']['state']=='disabled'
    dashboard.sample()
    assert dashboard.snapshot()['host']['cpu_percent']==30
    assert len(dashboard.snapshot()['history'])==2
    for _ in range(125):dashboard.sample()
    assert len(dashboard.snapshot()['history'])==dashboard.history_limit
    assert not dashboard.store.list()


def test_shared_mcp_health_and_process_status_without_restarts(config,repositories,resource_repos,monkeypatch):
    item=repositories.install(str(resource_repos['finance']))
    before=repositories.health(item)['boot_id']
    dashboard=Dashboard(config,Scheduler(config))
    def forbidden(*args,**kwargs):raise AssertionError('read must not restart')
    monkeypatch.setattr(repositories.__class__,'ensure_service',forbidden)
    dashboard.sample()
    value=dashboard.snapshot()
    assert value['mcp'][0]['state']=='healthy'
    assert value['counts']['healthy_mcp']==1
    assert value['counts']['declared_mcp']==1
    assert value['counts']['running_instances']==0  # An idle MCP is not an Agent invocation.
    assert repositories.health(repositories.get(item['id']))['boot_id']==before
    repositories.stop(item['id'])
    dashboard.sample()
    assert dashboard.snapshot()['mcp'][0]['state']=='disabled'
    repositories.update(item['id'],enabled=1)
    dashboard.sample()
    value=dashboard.snapshot()
    assert value['mcp'][0]['state']=='down' and value['counts']['healthy_mcp']==0
    assert any(a['kind']=='mcp' for a in value['alerts'])
    assert repositories.get(item['id'])['pid']==item['pid']


def test_live_but_unhealthy_mcp_and_cache(config,repositories,resource_repos,monkeypatch):
    repositories.install(str(resource_repos['finance']))
    dashboard=Dashboard(config,Scheduler(config))
    calls=[]
    def unhealthy(item):
        calls.append(item['id'])
        return {'checked_at':time.time(),'healthy':False,'upstreams':{'domain':'unavailable'},'latency_ms':2}
    monkeypatch.setattr(dashboard,'_probe',unhealthy)
    dashboard.sample();dashboard.sample()
    data=dashboard.snapshot()
    assert len(calls)==1
    assert data['mcp'][0]['process_state']=='alive'
    assert data['mcp'][0]['state']=='unhealthy'
    assert data['mcp'][0]['upstreams'][0]['state']=='unavailable'
    assert data['counts']['healthy_mcp']==0


def test_repo_conversation_is_one_instance_not_two(config,repositories,resource_repos):
    item=repositories.install(str(resource_repos['finance']))
    chats=Conversations(config)
    chat=chats.create(item['id'])
    turn=chats.send(chat['id'],'FIXTURE_SLEEP','dashboard-count')
    until=time.time()+10
    while time.time()<until:
        current=chats.turn(turn['id'])
        if current['child_pid']:break
        time.sleep(.1)
    try:
        assert current['child_pid']
        dashboard=Dashboard(config,Scheduler(config))
        dashboard.sample()
        data=dashboard.snapshot()
        assert data['counts']['running_instances']==1,data
        assert data['counts']['active_runs']==0
        assert data['counts']['active_turns']==1
        assert data['counts']['occupied_slots']==1
        assert len(data['instances'])==1 and data['instances'][0]['kind']=='conversation'
        agent=next(a for a in data['agents'] if a['id']==item['id'])
        assert agent['running_instances']==1 and agent['conversations']==1
        mcp=data['mcp'][0]
        assert agent['resources']['rss_bytes']>mcp['resources']['rss_bytes']
        assert not data['last_24h']
    finally:
        chats.cancel(chat['id'])
        until=time.time()+10
        while time.time()<until and chats.turn(turn['id'])['status'] in {'queued','running'}:time.sleep(.1)


def test_sampling_failure_keeps_previous_snapshot_and_no_private_error(config,monkeypatch):
    dashboard=Dashboard(config,Scheduler(config))
    dashboard.sample()
    before=dashboard.snapshot()
    def fail():raise RuntimeError('fixture-private-detail')
    monkeypatch.setattr(dashboard,'_sample',fail)
    dashboard.sample()
    value=dashboard.snapshot()
    assert value['available'] and value['sampling_error']
    assert value['sampled_at']==before['sampled_at']
    assert 'fixture-private-detail' not in json.dumps(value)
    with dashboard.data_lock:dashboard.data['sampled_at']=time.time()-20
    assert dashboard.snapshot()['stale']
    fresh=Dashboard(config,Scheduler(config))
    monkeypatch.setattr(fresh,'_sample',fail)
    assert not fresh.snapshot()['available']


def test_http_auth_and_no_model_or_config_secrets(config,monkeypatch):
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN','fixture-dashboard-token')
    monkeypatch.setenv('DEEPSEEK_API_KEY','fixture-dashboard-model-secret')
    with TestClient(create_app(config,background=False)) as client:
        assert client.get('/dashboard').status_code==200
        assert client.get('/api/dashboard').status_code==401
        response=client.get('/api/dashboard',headers={'Authorization':'Bearer fixture-dashboard-token'})
        assert response.status_code==200 and response.json()['available']
        assert 'fixture-dashboard-model-secret' not in response.text
        assert 'fixture-dashboard-token' not in response.text
        assert str(config.path) not in response.text
        assert 'command' not in response.json() and 'runtime' not in response.json()
        assert client.get('/api/dashboard',headers={'Host':'untrusted.invalid'}).status_code==400


def test_recent_failures_and_queued_counts_are_not_model_instances(config,repo):
    scheduler=Scheduler(config)
    scheduler.register('queued',str(repo))
    scheduler.trigger('queued')
    job=scheduler.store.create({'repo':str(repo)})
    scheduler.store.update(job['id'],status='timed_out',finished_at=time.time(),error='private prompt / token detail')
    dashboard=Dashboard(config,scheduler)
    dashboard.sample()
    data=dashboard.snapshot()
    assert data['counts']['queued_runs']==1 and data['counts']['running_instances']==0
    assert data['last_24h']['timed_out']==1
    assert data['recent_failures'][0]['id']==job['id']
    assert 'private prompt' not in json.dumps(data)


def test_process_resources_deduplicate_nested_roots_and_reused_pid(monkeypatch):
    tick=[10.]
    births={i:float(i) for i in range(1,8)}
    parents={1:0,2:1,3:2,4:3,5:4,6:1,7:6}
    seconds={i:float(i) for i in parents}
    class Fake:
        def __init__(self,pid):self.pid=pid
        def is_running(self):return True
        def status(self):return 'running'
        def create_time(self):return births[self.pid]
        def ppid(self):return parents[self.pid]
        def oneshot(self):return nullcontext()
        def memory_info(self):return SimpleNamespace(rss=self.pid*100)
        def cpu_times(self):return SimpleNamespace(user=seconds[self.pid],system=0)
        def children(self,recursive=False):
            result=[]
            def walk(pid):
                for child,parent in parents.items():
                    if parent==pid:
                        result.append(Fake(child))
                        if recursive:walk(child)
            walk(self.pid)
            return result
    monkeypatch.setattr(module,'psutil',SimpleNamespace(**{**vars(psutil),'Process':Fake}))
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:tick[0],time=time.time))
    sampler=ProcessSampler()
    roots=[{'pid':1,'birth':1.,'owner':'_service'},{'pid':2,'birth':2.,'owner':'admin'},
           {'pid':3,'birth':3.,'owner':'repo'},{'pid':4,'birth':4.,'owner':'repo'},{'pid':6,'birth':6.,'owner':'repo','category':'mcp'}]
    groups,total,states=sampler.collect(roots,4)
    assert total['process_count']==7 and total['rss_bytes']==2800
    assert total['cpu_percent'] is None and not total['cpu_complete']
    assert groups['admin']['rss_bytes']==200
    assert groups['repo']['rss_bytes']==2500
    assert sampler.categories[('repo','mcp')]['rss_bytes']==1300
    tick[0]=13
    for pid in seconds:seconds[pid]+=1
    groups,total,states=sampler.collect(roots,4)
    assert total['cpu_percent']==pytest.approx(58.33,.001)
    births[4]=999.
    tick[0]=16
    _,_,states=sampler.collect(roots,4)
    assert states[(4,4.)]=='missing'


def test_inaccessible_memory_is_unknown_not_zero(monkeypatch):
    class Fake:
        pid=1
        def is_running(self):return True
        def status(self):return 'running'
        def create_time(self):return 1.
        def ppid(self):return 0
        def oneshot(self):return nullcontext()
        def memory_info(self):raise psutil.AccessDenied(1)
        def cpu_times(self):raise psutil.AccessDenied(1)
        def children(self,recursive=False):return []
    monkeypatch.setattr(module,'psutil',SimpleNamespace(**{**vars(psutil),'Process':lambda _:Fake()}))
    _,resources,_=ProcessSampler().collect([{'pid':1,'birth':1.,'owner':'admin'}],2)
    assert resources['rss_bytes'] is None and resources['cpu_percent'] is None
    assert not resources['rss_complete'] and resources['unreadable']==2
    assert pid_state(1,2.)=='missing'
    assert pid_state('invalid',None)=='missing'


@pytest.mark.skipif(os.name=='nt',reason='POSIX SIGINT lifecycle')
def test_direct_admin_cli_is_observed_and_clears_metadata(config):
    child=subprocess.Popen([sys.executable,'-m','agent_base','--config',str(config.path),'admin','--session','dashboard','--message','FIXTURE_SLEEP'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    file=config.data/'admin/dashboard/process.json'
    try:
        until=time.time()+10
        while time.time()<until and not file.exists():time.sleep(.1)
        assert file.exists()
        dashboard=Dashboard(config,Scheduler(config));dashboard.sample()
        snapshot=dashboard.snapshot()
        assert snapshot['counts']['running_instances']==1
        assert snapshot['instances'][0]['kind']=='admin_cli'
        child.send_signal(signal.SIGINT)
        child.communicate(timeout=15)
        assert not file.exists()
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            child.communicate(timeout=15)
