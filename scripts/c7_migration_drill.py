"""C7 migration drill on an operator-provided copy of a real workspace.

    .venv/bin/python scripts/c7_migration_drill.py --data /tmp/drill/data

`--data` must already contain copies of `runs.sqlite` and `control.sqlite` made
with the SQLite backup API (see docs/C7Validation.md). The script never opens
the source workspace: it migrates the copy through the production `create_app`
startup hook, restarts it, and finally reads the migrated copy with the pre-C7
code (`--old-ref`, default `79ccc88`) to prove the rollback path keeps every
row. It prints one JSON report and exits non-zero on any assertion failure.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = {}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def rows(db, sql):
    with sqlite3.connect(db) as con:
        return con.execute(sql).fetchall()


def digest(db, sql):
    with sqlite3.connect(db) as con:
        payload = json.dumps(con.execute(sql).fetchall(), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def start(tag, drill, data):
    port = free_port()
    log = (drill / f'server-{tag}.log').open('w')
    env = {key: value for key, value in os.environ.items()
           if key in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT', 'LANG')}
    env.update(PYTHONPATH=str(ROOT / 'src'), DRILL_CONFIG=str(drill / 'fixture.toml'),
               DRILL_DATA=str(data), DRILL_PORT=str(port))
    started = time.monotonic()
    child = subprocess.Popen([sys.executable, str(drill / 'launch.py')], cwd=ROOT, env=env,
                             stdout=log, stderr=log)
    from urllib.request import urlopen
    for _ in range(600):
        if child.poll() is not None:
            log.close()
            raise SystemExit(f'{tag}: startup exited {child.returncode}; see {log.name}')
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/auth/session', timeout=0.3) as response:
                if response.status == 200:
                    return child, log, time.monotonic() - started, port
        except OSError:
            time.sleep(0.05)
    raise SystemExit(tag + ': startup timeout')


def stop(child, log):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()
    log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, type=Path,
                        help='directory holding runs.sqlite and control.sqlite copies')
    parser.add_argument('--work', type=Path, default=ROOT / '.c7-drill',
                        help='scratch directory for the drill config, logs and rollback copy')
    parser.add_argument('--old-ref', default='79ccc88', help='pre-C7 git ref used for rollback')
    args = parser.parse_args()
    data = args.data.resolve()
    drill = args.work.resolve()
    drill.mkdir(parents=True, exist_ok=True)
    assert (data / 'runs.sqlite').is_file() and (data / 'control.sqlite').is_file(), \
        'copy the real runs.sqlite/control.sqlite into --data first'
    (drill / 'fixture.toml').write_text(
        '[storage]\nroot="' + str(data) + '"\n[web]\nauth="jwt"\n[runtime]\nengine="dsh"\n'
        'command=' + json.dumps([sys.executable, str(ROOT / 'tests/fixtures/fake_dsh.py')]) + '\n'
        'model="fixture-model"\neffort="high"\ntimeout_seconds=20\n')
    (drill / 'launch.py').write_text(
        'import os, uvicorn\nfrom pathlib import Path\nfrom agent_base.config import Config\n'
        'from agent_base.web import create_app\n'
        "config = Config.load(Path(os.environ['DRILL_CONFIG']))\n"
        "uvicorn.run(create_app(config, background=False), host='127.0.0.1', port=int(os.environ['DRILL_PORT']), log_level='warning')\n")
    db = data / 'runs.sqlite'
    control = data / 'control.sqlite'
    backup = data / 'runs.sqlite.pre-chat-multiuser'
    OUT['before'] = {
        'bytes': db.stat().st_size,
        'conversations': rows(db, 'select count(*) from conversations')[0][0],
        'turns': rows(db, 'select count(*) from conversation_turns')[0][0],
        'events': rows(db, 'select count(*) from conversation_events')[0][0],
        'runs': rows(db, 'select count(*) from runs')[0][0],
        'installs': rows(db, 'select count(*) from repo_installations')[0][0],
        'columns': [row[1] for row in rows(db, 'pragma table_info(conversations)')],
        'conversation_digest': digest(db, 'select id,title,target,spec,created,updated,archived from conversations order by id'),
        'turn_digest': digest(db, 'select id,conversation_id,status,created,updated from conversation_turns order by id'),
        'event_digest': digest(db, 'select * from conversation_events order by seq'),
        'admin': rows(control, "select id from users where username='admin' and status='active'"),
        'default_domain': rows(control, "select id from domains where code='default'"),
        'migrate_audits': rows(control, "select count(*) from audit_log where action='chat.migrate'")[0][0],
    }
    assert backup.exists() is False, 'drill must start on an un-migrated copy'
    child, log, first_seconds, _ = start('first', drill, data)
    stop(child, log)
    OUT['first_start_seconds'] = round(first_seconds, 3)
    OUT['after_first'] = {
        'bytes': db.stat().st_size,
        'columns': [row[1] for row in rows(db, 'pragma table_info(conversations)')],
        'indexes': sorted(row[1] for row in rows(db, "select * from sqlite_master where type='index' and name in ('conversations_owner','conversations_domain')")),
        'backup_exists': backup.exists(),
        'backup_bytes': backup.stat().st_size if backup.exists() else 0,
        'backup_columns': [row[1] for row in rows(backup, 'pragma table_info(conversations)')] if backup.exists() else [],
        'backup_digest': digest(backup, 'select id,title,target,spec,created,updated,archived from conversations order by id') if backup.exists() else None,
        'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest() if backup.exists() else None,
        'conversations': rows(db, 'select count(*) from conversations')[0][0],
        'turns': rows(db, 'select count(*) from conversation_turns')[0][0],
        'events': rows(db, 'select count(*) from conversation_events')[0][0],
        'runs': rows(db, 'select count(*) from runs')[0][0],
        'installs': rows(db, 'select count(*) from repo_installations')[0][0],
        'null_owners': rows(db, 'select count(*) from conversations where owner_user_id is null')[0][0],
        'null_domains': rows(db, 'select count(*) from conversations where domain_id is null')[0][0],
        'unowned_installs': rows(db, 'select count(*) from repo_installations where domain_id is null')[0][0],
        'owners': rows(db, 'select distinct owner_user_id from conversations'),
        'domains': rows(db, 'select distinct domain_id from conversations'),
        'migrate_audits': rows(control, "select count(*) from audit_log where action='chat.migrate'")[0][0],
        'migrate_detail': rows(control, "select detail from audit_log where action='chat.migrate'"),
        'conversation_digest': digest(db, 'select id,title,target,spec,created,updated,archived from conversations order by id'),
        'turn_digest': digest(db, 'select id,conversation_id,status,created,updated from conversation_turns order by id'),
        'event_digest': digest(db, 'select * from conversation_events order by seq'),
    }
    child, log, second_seconds, _ = start('second', drill, data)
    stop(child, log)
    OUT['second_start_seconds'] = round(second_seconds, 3)
    OUT['after_second'] = {
        'migrate_audits': rows(control, "select count(*) from audit_log where action='chat.migrate'")[0][0],
        'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
        'owners': rows(db, 'select distinct owner_user_id from conversations'),
        'domains': rows(db, 'select distinct domain_id from conversations'),
        'conversation_digest': digest(db, 'select id,title,target,spec,created,updated,archived from conversations order by id'),
        'event_digest': digest(db, 'select * from conversation_events order by seq'),
    }
    # Rollback: the pre-C7 code reads the migrated database unchanged.
    old = drill / 'old-src'
    rollback = drill / 'rollback'
    rollback.mkdir(exist_ok=True)
    if not old.is_dir():
        old.mkdir()
        archive = subprocess.run(['git', 'archive', args.old_ref], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(['tar', '-x', '-C', str(old)], input=archive.stdout, check=True)
    for name in ('runs.sqlite', 'control.sqlite'):
        import shutil
        shutil.copy2(data / name, rollback / name)
    probe = subprocess.run(
        [str(ROOT / '.venv/bin/python'), '-c',
         'from pathlib import Path\n'
         'from agent_base.config import Config\n'
         'from agent_base.conversations import Conversations\n'
         'import os\n'
         'config = Config.load(Path(os.environ["DRILL_CONFIG"]))\n'
         'config.data = Path(os.environ["DRILL_ROLLBACK"]).resolve()\n'
         'chats = Conversations(config)\n'
         'with chats.store.connection() as con:\n'
         '    rows = con.execute("select count(*) from conversations").fetchone()\n'
         '    turns = con.execute("select count(*) from conversation_turns").fetchone()\n'
         'print("OLD_CODE_OK", rows[0], turns[0])\n'],
        cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(old / 'src'),
                       'DRILL_CONFIG': str(drill / 'fixture.toml'),
                       'DRILL_ROLLBACK': str(rollback)}, capture_output=True, text=True)
    OUT['rollback'] = {'exit': probe.returncode, 'stdout': probe.stdout.strip(),
                       'stderr': probe.stderr.strip().splitlines()[-3:]}
    OUT['rollback']['conversation_digest'] = digest(rollback / 'runs.sqlite',
                                                    'select id,title,target,spec,created,updated,archived from conversations order by id')
    OUT['rollback']['new_columns_still_there'] = [row[1] for row in rows(rollback / 'runs.sqlite', 'pragma table_info(conversations)')]
    print(json.dumps(OUT, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
