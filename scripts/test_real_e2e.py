"""Isolated real-process runner. Run after uv sync --frozen and npm ci/build."""
import hashlib, json, os, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
import signal
def _cancel(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, _cancel)
from urllib.request import urlopen
P=Path(__file__).resolve().parents[1]
report=Path(os.environ.get('CI_REPORT',str(P/'frontend/test-results/real-processes'))); report.mkdir(parents=True,exist_ok=True)
env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','SYSTEMROOT','CI_REPORT','PLAYWRIGHT_BROWSERS_PATH') if k in os.environ}
env['CI_REPORT']=str(report)
env.update(PYTHONPATH=str(P/'src'),AGENT_BASE_WEB_AUTH='jwt',E2E_PYTHON=sys.executable)
def stop(child,log):
    if child.poll() is None:
        child.terminate()
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
    log.close()
def start(label,failure=False):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    env.update(E2E_PORT=str(port),E2E_URL=f'http://127.0.0.1:{port}')
    log=(report/f'{label}.log').open('w')
    child=subprocess.Popen([sys.executable,'tests/e2e/server.py'],cwd=P,env=env,stdout=log,stderr=log)
    try:
        for _ in range(150):
            if child.poll() is not None:
                assert failure and child.returncode != 0, f'{label}: unexpected exit; see logs'
                try: urlopen(env['E2E_URL']+'/api/auth/session',timeout=.2)
                except OSError: return child,log
                raise AssertionError('failed process still available')
            try:
                with urlopen(env['E2E_URL']+'/api/auth/session',timeout=.2) as r: assert r.status==200
            except OSError: time.sleep(.1); continue
            assert not failure,'corrupt database served requests'
            return child,log
        raise TimeoutError(label)
    except BaseException: stop(child,log); raise
with tempfile.TemporaryDirectory(prefix='agent-base-real-e2e-') as directory:
    root=Path(directory); config=root/'fixture.toml'
    config.write_text('[storage]\nroot="data"\n[runtime]\nengine="dsh"\ncommand=REPLACE_COMMAND\nmodel="fixture-model"\neffort="high"\ntimeout_seconds=20\n'.replace('REPLACE_COMMAND',json.dumps([sys.executable,str(P/'tests/fixtures/fake_dsh.py')])) )
    env.update(E2E_CONFIG=str(config),E2E_CONTROL=str(root/'data/control.sqlite'),E2E_DATA=str(root/'data/runs.sqlite'))
    subprocess.run([sys.executable,'tests/e2e/seed.py',str(config)],cwd=P,env=env,check=True)
    child,log=start('first-start'); stop(child,log)
    db=root/'data/runs.sqlite'
    with sqlite3.connect(root/'data/control.sqlite') as control, sqlite3.connect(db) as data:
        for name in ('developer','peer','foreign','reader','revoked'):
            user=control.execute('SELECT id,home_domain_id FROM users WHERE username=?',(name,)).fetchone()
            data.execute('UPDATE conversations SET owner_user_id=?,domain_id=? WHERE id=?',(*user,name+'-chat'))
        default=control.execute("SELECT id FROM domains WHERE code='default'").fetchone()[0]
        other=control.execute("SELECT id FROM domains WHERE code='other'").fetchone()[0]
        data.execute("UPDATE repo_installations SET domain_id=? WHERE id='default-agent'",(default,))
        data.execute("UPDATE repo_installations SET domain_id=? WHERE id='other-agent'",(other,))
    backup=root/'data/runs.sqlite.pre-chat-multiuser'
    original=hashlib.sha256(backup.read_bytes()).hexdigest()
    with sqlite3.connect(backup) as c:
        assert 'owner_user_id' not in [r[1] for r in c.execute('PRAGMA table_info(conversations)')]
        assert c.execute("SELECT id FROM conversations WHERE id='legacy'").fetchone()[0]=='legacy'
    with sqlite3.connect(db) as c:
        before=c.execute('SELECT owner_user_id,domain_id FROM conversations').fetchall(); assert all(before[0])
    child,log=start('second-start')
    try:
        assert hashlib.sha256(backup.read_bytes()).hexdigest()==original
        with sqlite3.connect(db) as c: assert c.execute('SELECT owner_user_id,domain_id FROM conversations').fetchall()==before
        with sqlite3.connect(root/'data/control.sqlite') as c: assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='chat.migrate'").fetchone()[0]==1
        match='contracts' if '--contracts' in sys.argv else 'auth|chat_authz|chat_write'
        result=subprocess.run(['npx','--no-install','playwright','test','--config','playwright.real.config.js',match],cwd=P/'frontend',env=env)
    finally:
        stop(child,log)
        subprocess.run([sys.executable,'tests/e2e/cleanup.py',str(config)],cwd=P,env=env,check=True)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE conversations SET owner_user_id=NULL WHERE id='legacy'")
        c.execute("CREATE TRIGGER e2e_fail_migration BEFORE UPDATE ON conversations BEGIN SELECT RAISE(ABORT, 'E2E migration storage failure'); END")
    child,log=start('failed-start',True); stop(child,log)
    (report/'summary.json').write_text(json.dumps(dict(migration_backup=True,restart_idempotent=True,failed_start_unavailable=True,playwright_exit=result.returncode),indent=2))
    sys.exit(result.returncode)
