import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_base.scheduler import Scheduler
from agent_base.schedules import Schedules
from agent_base.web import create_app


def selection(repositories, resource_repos, **extra):
    item=repositories.install(str(resource_repos['finance']))
    future=datetime.now(timezone.utc)+timedelta(days=2)
    return {'agent_id':item['id'],'title':'定时财务汇总','frequency':'once','date':future.strftime('%Y-%m-%d'),
            'time':'09:30','timezone':'UTC','weekdays':[],**extra}


def test_once_schedule_pins_install_and_runs_entrypoint_once(config,repositories,resource_repos):
    scheduler=Scheduler(config)
    schedules=Schedules(scheduler)
    data=selection(repositories,resource_repos,model='selected-model',effort='low')
    preview=schedules.preview(data)
    plan=schedules.create(data)
    work=scheduler.work(plan['id'])
    assert work['definition']['spec']['installation_id']==data['agent_id']
    assert work['next_due']==preview['next_times'][0]
    assert work['definition']['spec']['runtime']['model']=='selected-model'
    when=work['next_due']+1
    with ThreadPoolExecutor(max_workers=2) as pool:
        batches=list(pool.map(lambda _:Scheduler(config).cron_tick(now=when),range(2)))
    assert sum(len(batch) for batch in batches)==1
    assert len(scheduler.triggers(plan['id']))==1
    assert scheduler.cron_tick(now=when+100000000)==[]
    ended=scheduler.work(plan['id'])
    assert ended['next_due'] is None and not ended['enabled']
    with pytest.raises(ValueError,match='已触发'):
        schedules.enable(plan['id'],True)
    scheduler.dispatch()
    run=schedules.runs(plan['id'])[0]
    result=scheduler.service.wait(run['id'],20)
    assert result['status']=='succeeded',result
    assert result['result']['agent_report']['status']=='completed'
    prompt=(Path(result['root'])/'prompt.txt').read_text()
    assert '<entrypoint>' in prompt and '<agent_base_report_contract>' in prompt
    assert schedules.list()[0]['consumed'] is True


def test_periodic_preview_and_pause_resume(config,repositories,resource_repos):
    schedules=Schedules(Scheduler(config))
    data=selection(repositories,resource_repos,frequency='weekly',weekdays=[1,3,5],timezone='Asia/Shanghai')
    preview=schedules.preview(data)
    assert preview['cron']=='30 9 * * 1,3,5' and len(preview['next_times'])==3
    assert preview['next_times']==sorted(preview['next_times'])
    plan=schedules.create(data)
    assert schedules.enable(plan['id'],False)['next_due'] is None
    assert schedules.enable(plan['id'],True)['next_due']>0
    daily=schedules.preview({**data,'frequency':'daily'})
    assert daily['cron']=='30 9 * * *'


def test_once_pause_resume_and_timezone(config,repositories,resource_repos):
    schedules=Schedules(Scheduler(config))
    data=selection(repositories,resource_repos)
    utc=schedules.preview(data)['next_times'][0]
    shanghai=schedules.preview({**data,'timezone':'Asia/Shanghai'})['next_times'][0]
    assert utc-shanghai==8*3600
    plan=schedules.create(data)
    schedules.enable(plan['id'],False)
    assert schedules.enable(plan['id'],True)['next_due']==utc


@pytest.mark.parametrize('patch,message',[
    ({'date':'2000-01-01'},'晚于当前'),
    ({'date':'not-a-date'},'日期'),
    ({'time':'99:99'},'时间'),
    ({'timezone':'missing/zone'},'时区'),
    ({'frequency':'weekly','weekdays':[]},'星期'),
    ({'frequency':'weekly','weekdays':[8]},'星期'),
    ({'date':'2099-03-08','time':'02:30','timezone':'America/New_York'},'跳变'),
    ({'date':'2099-11-01','time':'01:30','timezone':'America/New_York'},'两次'),
])
def test_invalid_schedule_is_rejected(config,repositories,resource_repos,patch,message):
    schedules=Schedules(Scheduler(config))
    with pytest.raises(ValueError,match=message):
        schedules.preview(selection(repositories,resource_repos,**patch))


def test_no_entrypoint_and_disabled_install_are_rejected(config,repositories,resource_repos):
    repo=resource_repos['finance']
    (repo/'entrypoint.md').unlink()
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                    '-c','core.hooksPath='+os.devnull,'commit','--quiet','-m','remove entrypoint'],check=True)
    data=selection(repositories,resource_repos)
    schedules=Schedules(Scheduler(config))
    assert not schedules.catalog()['agents'][0]['available']
    with pytest.raises(ValueError,match='entrypoint'):
        schedules.create(data)
    repositories.stop(data['agent_id'])
    with pytest.raises(ValueError,match='停用'):
        schedules.create(data)


def test_schedule_http_validation_auth_and_report(config,repositories,resource_repos,monkeypatch):
    data=selection(repositories,resource_repos)
    monkeypatch.setenv('AGENT_BASE_WEB_TOKEN','fixture-schedule-token')
    app=create_app(config,background=False)
    headers={'Authorization':'Bearer fixture-schedule-token'}
    with TestClient(app) as client:
        assert client.get('/cron').status_code==200
        assert client.get('/api/schedules').status_code==401
        assert client.post('/api/schedules',json=data).status_code==401
        assert client.post('/api/schedules',headers={**headers,'Origin':'https://untrusted.invalid'},json=data).status_code==403
        assert client.post('/api/schedules/preview',headers=headers,json=data).status_code==200
        response=client.post('/api/schedules',headers=headers,json=data)
        assert response.status_code==201,response.text
        plan=response.json()
        assert client.get('/api/schedules',headers=headers).json()[0]['id']==plan['id']
        assert client.post('/api/schedules/'+plan['id']+'/pause',headers=headers).status_code==200
        assert client.post('/api/schedules/'+plan['id']+'/resume',headers=headers).status_code==200
        assert client.get('/api/schedules/'+plan['id']+'/runs',headers=headers).json()==[]
        when=app.state.scheduler.work(plan['id'])['next_due']+1
        app.state.scheduler.cron_tick(now=when)
        app.state.scheduler.dispatch()
        run=client.get('/api/schedules/'+plan['id']+'/runs',headers=headers).json()[0]
        final=app.state.scheduler.service.wait(run['id'],20)
        assert final['status']=='succeeded',final
        report=client.get('/api/runs/'+run['id']+'/report',headers=headers).json()
        assert report['status']=='succeeded' and report['agent_report']['status']=='completed'


def test_once_registration_rejects_past_and_mixed_cron(config,repo):
    scheduler=Scheduler(config)
    for value in (-1,float('inf'),float('nan'),True):
        with pytest.raises(ValueError,match='一次性'):
            scheduler.register('bad',str(repo),run_at=value)
    with pytest.raises(ValueError,match='一次性'):
        scheduler.register('mixed',str(repo),run_at=9999999999,cron='* * * * *')
