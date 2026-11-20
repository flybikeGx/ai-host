"""Exercise report/exit-code contracts through the real CLI in disposable repos."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def checkout(tmp_path):
    (tmp_path/'scripts').mkdir()
    shutil.copy(Path(__file__).resolve().parents[1]/'scripts/ci.py',tmp_path/'scripts/ci.py')
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@example.invalid','-c','core.hooksPath=/dev/null','commit','--allow-empty','-qm','fixture'],check=True)
    return tmp_path


def invoke(root, *options):
    process=subprocess.run([sys.executable,str(root/'scripts/ci.py'),'quick',*options],capture_output=True,text=True)
    report=json.loads(next((root/'.ci-reports').glob('*/report.json')).read_text())
    return process.returncode,report


def test_missing_dependency_records_environment_error(checkout):
    code,report=invoke(checkout)
    assert code==2 and report['status']=='environment_error'
    assert report['stages']==[]


@pytest.mark.parametrize('body,options,expected,status',[
    ('exit 7',[],1,'failed'),
    ('sleep 10',['--timeout','.01'],124,'timeout'),
])
def test_failed_and_timed_out_stage(checkout,body,options,expected,status):
    (checkout/'frontend/node_modules').mkdir(parents=True)
    binary=checkout/'.venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\n'+body+'\n')
    binary.chmod(0o755)
    code,report=invoke(checkout,*options)
    assert code==expected and report['status']==status
    assert report['stages'][0]['name']=='unit'
    if expected==1:
        assert report['stages'][0]['exit_code']==7


def test_cancelled_run_retains_report_and_stops_stage(checkout):
    import signal
    import time
    (checkout/'frontend/node_modules').mkdir(parents=True)
    binary=checkout/'.venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nsleep 30\n')
    binary.chmod(0o755)
    child=subprocess.Popen([sys.executable,str(checkout/'scripts/ci.py'),'quick'],stdout=subprocess.PIPE,text=True)
    try:
        for _ in range(100):
            if list((checkout/'.ci-reports').glob('*/unit.log')):
                break
            time.sleep(.05)
        else:
            pytest.fail('stage did not start')
        child.send_signal(signal.SIGINT)
        child.communicate(timeout=15)
        assert child.returncode==130
        report=json.loads(next((checkout/'.ci-reports').glob('*/report.json')).read_text())
        assert report['status']=='cancelled'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_hooks_block_commit_and_uninstall(checkout):
    source=Path(__file__).resolve().parents[1]
    shutil.copy(source/'scripts/ci_hooks.py',checkout/'scripts/ci_hooks.py')
    shutil.copytree(source/'.githooks',checkout/'.githooks')
    (checkout/'frontend/node_modules').mkdir(parents=True)
    binary=checkout/'.venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexit 7\n')
    binary.chmod(0o755)
    def git(*args):
        return subprocess.run(['git','-C',str(checkout),*args],capture_output=True,text=True)
    before=git('rev-parse','HEAD').stdout
    subprocess.run([sys.executable,str(checkout/'scripts/ci_hooks.py'),'install'],check=True)
    attempt=git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-m','must fail')
    assert attempt.returncode!=0
    assert git('rev-parse','HEAD').stdout==before
    assert next((checkout/'.ci-reports').glob('*/report.json')).exists()
    push=subprocess.run([str(checkout/'.githooks/pre-push')],cwd=checkout,capture_output=True,text=True)
    assert push.returncode==1
    subprocess.run([sys.executable,str(checkout/'scripts/ci_hooks.py'),'uninstall'],check=True)
    assert git('config','--worktree','--get','core.hooksPath').returncode==1


def test_cancel_during_process_creation_is_deferred(checkout, monkeypatch):
    import importlib.util
    import os
    import signal
    spec=importlib.util.spec_from_file_location('local_ci',checkout/'scripts/ci.py')
    ci=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ci)
    (checkout/'frontend/node_modules').mkdir(parents=True)
    binary=checkout/'.venv/bin/python'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nsleep 30\n')
    binary.chmod(0o755)
    original=ci.subprocess.Popen
    def interrupted_spawn(command,*args,**kwargs):
        if command[0]==str(binary):
            os.kill(os.getpid(),signal.SIGINT)
        return original(command,*args,**kwargs)
    monkeypatch.setattr(ci.subprocess,'Popen',interrupted_spawn)
    monkeypatch.setattr(sys,'argv',['ci.py','quick'])
    previous={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
    try:
        assert ci.main()==130
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)
    report=json.loads(next((checkout/'.ci-reports').glob('*/report.json')).read_text())
    assert report['status']=='cancelled'
    assert report['stages'][0]['exit_code'] is not None


def test_stage_cleans_detached_descendant(tmp_path):
    import psutil
    probe=tmp_path/'probe.py'
    pidfile=tmp_path/'pid'
    probe.write_text("import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)\nPath(sys.argv[1]).write_text(str(p.pid))\ntime.sleep(.5)\n")
    helper=Path(__file__).resolve().parents[1]/'scripts/ci_stage.py'
    subprocess.run([sys.executable,str(helper),str(tmp_path/'processes.json'),sys.executable,str(probe),str(pidfile)],check=True,timeout=12)
    pid=int(pidfile.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status()==psutil.STATUS_ZOMBIE
    assert json.loads((tmp_path/'processes.json').read_text())['tracked_children']==1
