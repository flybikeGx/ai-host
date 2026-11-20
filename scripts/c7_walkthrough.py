#!/usr/bin/env python3
"""C7 manual walkthrough on a real backend, three roles, disposable data.

Runs a real uvicorn process with the deterministic `tests/fixtures/fake_dsh.py`
runtime, real SQLite and real HTTP/SSE, then walks viewer / developer /
platform_admin (plus the service account and a NULL-owner conversation) through
login, domain selection, owner-only lists, send/SSE/stop, artifact download and
the platform-admin `scope=all` read-only bypass. Prints one `PASS`/`FAIL` line
per check and exits non-zero on the first failing contract set.

It never touches a real database or account: every run uses a new temp dir,
fresh SQLite files and an OS-assigned loopback port.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ('admin', 'fixture-browser-password')
MEMBER = 'fixture-member-password'
failures: list[str] = []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Client:
    def __init__(self, base: str):
        self.base = base
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, method=method,
                          headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, method, path, body=None, headers=None):
        status, _, payload = self.request(method, path, body, headers)
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload

    def login(self, username, password):
        status, payload = self.json('POST', '/api/auth/login',
                                    {'username': username, 'password': password})
        return status, payload


def check(role: str, label: str, ok: bool, detail: str = ''):
    print(f"{'PASS' if ok else 'FAIL'} [{role}] {label}" + (f" -> {detail}" if detail else ''), flush=True)
    if not ok:
        failures.append(f"{role}: {label} ({detail})")


def wait_ready(base: str, child: subprocess.Popen, log) -> None:
    from urllib.request import urlopen
    for _ in range(300):
        if child.poll() is not None:
            log.close()
            raise SystemExit(f'server exited {child.returncode}; see {log.name}')
        try:
            with urlopen(base + '/api/auth/session', timeout=0.3) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise SystemExit('server did not become ready')


def main() -> int:
    directory = Path(tempfile.mkdtemp(prefix='agent-base-c7-walkthrough-'))
    config = directory / 'fixture.toml'
    config.write_text('[storage]\nroot="data"\n[runtime]\nengine="dsh"\n'
                      'command=' + json.dumps([sys.executable, str(ROOT / 'tests/fixtures/fake_dsh.py')]) + '\n'
                      'model="fixture-model"\neffort="high"\ntimeout_seconds=20\n')
    environment = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT', 'LANG')}
    environment.update(PYTHONPATH=str(ROOT / 'src'), AGENT_BASE_WEB_AUTH='jwt', E2E_PYTHON=sys.executable,
                       E2E_CONFIG=str(config), E2E_PORT=str(free_port()),
                       E2E_CONTROL=str(directory / 'data/control.sqlite'),
                       E2E_DATA=str(directory / 'data/runs.sqlite'))
    base = 'http://127.0.0.1:' + environment['E2E_PORT']
    subprocess.run([sys.executable, 'tests/e2e/seed.py', str(config)], cwd=ROOT, env=environment,
                   check=True, stdout=subprocess.DEVNULL)
    # seed.py leaves conversation specs empty; the walkthrough sends through the
    # deterministic fake runtime, so give the seeded conversations a real spec
    # and add a viewer-owned conversation (seed.py only owns C3/C4 fixtures).
    runtime_spec = json.dumps({'runtime': {'engine': 'dsh',
                                           'command': [sys.executable, str(ROOT / 'tests/fixtures/fake_dsh.py')],
                                           'model': 'fixture-model', 'effort': 'high', 'timeout_seconds': 20},
                               'auto_title': False, 'agent_name': 'Admin Agent'})
    with sqlite3.connect(directory / 'data/runs.sqlite') as data:
        data.execute('INSERT INTO conversations(id,title,target,spec,created,updated) VALUES(?,?,?,?,?,?)',
                     ('viewer-chat', 'viewer private', 'admin', runtime_spec, time.time(), time.time()))
        data.execute('UPDATE conversations SET spec=?', (runtime_spec,))
    viewer_artifacts = directory / 'data/conversations/viewer-chat/workspace/artifacts'
    viewer_artifacts.mkdir(parents=True)
    (viewer_artifacts / 'report.txt').write_text('viewer isolated artifact\n')
    child = None
    log = None
    try:
        log = (directory / 'server.log').open('w')
        child = subprocess.Popen([sys.executable, 'tests/e2e/server.py'], cwd=ROOT, env=environment,
                                 stdout=log, stderr=log)
        wait_ready(base, child, log)
        child.terminate(); child.wait(timeout=15); child = None
        with sqlite3.connect(directory / 'data/runs.sqlite') as data, \
                sqlite3.connect(directory / 'data/control.sqlite') as control:
            for name in ('viewer', 'developer', 'peer', 'foreign', 'reader', 'revoked'):
                user = control.execute('SELECT id,home_domain_id FROM users WHERE username=?', (name,)).fetchone()
                data.execute('UPDATE conversations SET owner_user_id=?,domain_id=? WHERE id=?',
                             (*user, name + '-chat'))
            default = control.execute("SELECT id FROM domains WHERE code='default'").fetchone()[0]
            other = control.execute("SELECT id FROM domains WHERE code='other'").fetchone()[0]
            data.execute("UPDATE repo_installations SET domain_id=? WHERE id='default-agent'", (default,))
            data.execute("UPDATE repo_installations SET domain_id=? WHERE id='other-agent'", (other,))
        child_log = (directory / 'walkthrough-server.log').open('w')
        child = subprocess.Popen([sys.executable, 'tests/e2e/server.py'], cwd=ROOT, env=environment,
                                 stdout=child_log, stderr=child_log)
        wait_ready(base, child, child_log)
        with sqlite3.connect(directory / 'data/control.sqlite') as control:
            admin_id = control.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            default = control.execute("SELECT id FROM domains WHERE code='default'").fetchone()[0]

        # --- viewer: read-only, own data only ---------------------------------
        viewer = Client(base)
        status, payload = viewer.login('viewer', MEMBER)
        check('viewer', 'login', status == 200 and payload.get('authenticated') is True, f'HTTP {status}')
        status, me = viewer.json('GET', '/api/v1/me', headers={'x-domain-id': default})
        permissions = set((me.get('domains') or [{}])[0].get('permissions', [])) if status == 200 else set()
        check('viewer', '/me domain permissions', status == 200 and 'agent.run' not in permissions
              and 'task.write' not in permissions, f'HTTP {status} permissions={sorted(permissions)[:4]}...')
        status, targets = viewer.json('GET', '/api/chat/targets', headers={'x-domain-id': default})
        names = [entry.get('name') for entry in targets.get('targets', [])] if status == 200 else []
        check('viewer', 'targets in own domain only, no Admin Agent',
              status == 200 and 'Admin Agent' not in names and 'Other Agent' not in names, f'{names}')
        status, listed = viewer.json('GET', '/api/conversations', headers={'x-domain-id': default})
        ids = [row.get('id') for row in listed] if status == 200 else []
        check('viewer', 'list shows only own conversation', status == 200 and ids == ['viewer-chat'], f'{ids}')
        status, payload = viewer.json('POST', '/api/conversations', {'target': 'default-agent'},
                                      headers={'x-domain-id': default})
        check('viewer', 'create denied', status == 403 and payload.get('code') == 'permission_denied',
              f'HTTP {status} {payload.get("code")}')
        status, payload = viewer.json('GET', '/api/conversations/peer-chat', headers={'x-domain-id': default})
        check('viewer', "other user's conversation is 404", status == 404, f'HTTP {status}')
        status, _, body = viewer.request('GET', '/api/conversations/viewer-chat/download?path=artifacts/report.txt',
                                         headers={'x-domain-id': default})
        check('viewer', 'own artifact download', status == 200 and b'viewer isolated artifact' in body,
              f'HTTP {status}')

        # --- developer: writes own data, never other data or Admin Agent ------
        developer = Client(base)
        status, _ = developer.login('developer', MEMBER)
        check('developer', 'login', status == 200, f'HTTP {status}')
        status, listed = developer.json('GET', '/api/conversations', headers={'x-domain-id': default})
        ids = [row.get('id') for row in listed] if status == 200 else []
        check('developer', 'list excludes peers\' conversations', status == 200 and ids == ['developer-chat'], f'{ids}')
        status, targets = developer.json('GET', '/api/chat/targets', headers={'x-domain-id': default})
        names = [entry.get('name') for entry in targets.get('targets', [])] if status == 200 else []
        check('developer', 'Admin Agent and other-domain Agent absent',
              status == 200 and 'Admin Agent' not in names and 'Other Agent' not in names, f'{names}')
        status, payload = developer.json('GET', '/api/chat/targets', headers={'x-domain-id': 'missing-domain'})
        check('developer', 'unknown domain rejected', status == 403 and payload.get('code') == 'domain_forbidden',
              f'HTTP {status} {payload.get("code")}')
        status, created = developer.json('POST', '/api/conversations', {'target': 'default-agent', 'title': 'walkthrough'},
                                         headers={'x-domain-id': default})
        created_chat = created.get('id') if status == 201 else None
        check('developer', 'create own conversation in the selected domain',
              status == 201 and created_chat, f'HTTP {status} id={created_chat}')
        if created_chat:
            status, snapshot = developer.json('GET', f'/api/conversations/{created_chat}',
                                              headers={'x-domain-id': default})
            conversation = snapshot.get('conversation', {}) if status == 200 else {}
            check('developer', 'create stamps the selected domain',
                  conversation.get('domain_id') == default, f'domain_id={conversation.get("domain_id")}')
            status, _ = developer.json('PATCH', f'/api/conversations/{created_chat}', {'title': 'walkthrough renamed'},
                                       headers={'x-domain-id': default})
            check('developer', 'rename own conversation', status == 200, f'HTTP {status}')
            status, _ = developer.json('PATCH', f'/api/conversations/{created_chat}', {'archived': True},
                                       headers={'x-domain-id': default})
            check('developer', 'archive own conversation', status == 200, f'HTTP {status}')
        # The fixture installation has no runnable MCP service, so the send/SSE/
        # stop walkthrough runs on the seeded own conversation (admin target).
        chat = 'developer-chat'
        status, sent = developer.json('POST', f'/api/conversations/{chat}/messages',
                                      {'text': 'C7 walkthrough message', 'client_key': 'c7-walk-1'},
                                      headers={'x-domain-id': default})
        check('developer', 'send message', status == 202 and sent.get('id'), f'HTTP {status}')
        turn = None
        for _ in range(200):
            status, snapshot = developer.json('GET', f'/api/conversations/{chat}', headers={'x-domain-id': default})
            if status == 200 and snapshot.get('turns'):
                turn = snapshot['turns'][0]
                if turn['status'] in ('succeeded', 'failed', 'cancelled'):
                    break
            time.sleep(0.1)
        check('developer', 'real turn reached a terminal state', bool(turn) and turn['status'] == 'succeeded',
              f'status={turn and turn["status"]}')
        status, _, body = developer.request('GET', f'/api/conversations/{chat}/events?once=true',
                                            headers={'x-domain-id': default})
        check('developer', 'SSE replays persisted events', status == 200 and b'turn.' in body,
              f'HTTP {status} bytes={len(body)}')
        status, _, body = developer.request('GET', f'/api/conversations/{chat}/download?path=artifacts/report.txt',
                                            headers={'x-domain-id': default})
        check('developer', 'own artifact download', status == 200 and len(body) > 0,
              f'HTTP {status} bytes={len(body)}')
        status, payload = developer.json('PATCH', '/api/conversations/peer-chat', {'title': 'stolen'},
                                         headers={'x-domain-id': default})
        check('developer', 'write to another conversation is 404', status == 404, f'HTTP {status}')
        status, _ = developer.json('POST', f'/api/conversations/{chat}/messages',
                                   {'text': 'FIXTURE_SLEEP', 'client_key': 'c7-walk-sleep'},
                                   headers={'x-domain-id': default})
        check('developer', 'long turn accepted for the stop walkthrough', status == 202, f'HTTP {status}')
        status, _ = developer.json('POST', f'/api/conversations/{chat}/messages',
                                   {'text': 'conflict', 'client_key': 'c7-walk-conflict'},
                                   headers={'x-domain-id': default})
        check('developer', 'second active turn while busy is 409', status == 409, f'HTTP {status}')
        status, payload = developer.json('POST', f'/api/conversations/{chat}/stop', None,
                                         headers={'x-domain-id': default})
        check('developer', 'stop own turn', status == 200, f'HTTP {status}')
        for _ in range(200):
            status, snapshot = developer.json('GET', f'/api/conversations/{chat}', headers={'x-domain-id': default})
            if status == 200 and snapshot.get('turns') and snapshot['turns'][-1]['status'] in (
                    'succeeded', 'failed', 'cancelled'):
                break
            time.sleep(0.1)
        check('developer', 'turn cancelled after stop', snapshot['turns'][-1]['status'] == 'cancelled',
              f'status={snapshot["turns"][-1]["status"]}')

        # --- platform admin: Admin Agent plus read-only scope=all -------------
        admin = Client(base)
        status, _ = admin.login(*ADMIN)
        check('platform_admin', 'login', status == 200, f'HTTP {status}')
        status, me = admin.json('GET', '/api/v1/me', headers={'x-domain-id': default})
        check('platform_admin', '/me platform_admin flag', status == 200 and me.get('platform_admin') is True,
              f'HTTP {status} platform_admin={me.get("platform_admin")}')
        status, targets = admin.json('GET', '/api/chat/targets', headers={'x-domain-id': default})
        names = [entry.get('name') for entry in targets.get('targets', [])] if status == 200 else []
        check('platform_admin', 'Admin Agent visible', status == 200 and 'Admin Agent' in names, f'{names}')
        status, listed = admin.json('GET', '/api/conversations', headers={'x-domain-id': default})
        ids = [row.get('id') for row in listed] if status == 200 else []
        check('platform_admin', 'default list stays own (scope=own)', status == 200 and 'peer-chat' not in ids,
              f'{ids}')
        status, listed = admin.json('GET', '/api/conversations?scope=all', headers={'x-domain-id': default})
        ids = [row.get('id') for row in listed] if status == 200 else []
        check('platform_admin', 'scope=all lists foreign conversations', status == 200 and 'peer-chat' in ids,
              f'HTTP {status} count={len(ids)}')
        status, _, body = admin.request('GET', '/api/conversations/peer-chat/download?path=artifacts/report.txt&scope=all',
                                        headers={'x-domain-id': default})
        check('platform_admin', 'scope=all reads peer artifact', status == 200 and b'peer isolated artifact' in body,
              f'HTTP {status}')
        status, payload = admin.json('PATCH', '/api/conversations/peer-chat', {'title': 'admin edit'},
                                     headers={'x-domain-id': default})
        check('platform_admin', 'admin write bypass absent (PATCH 404)', status == 404, f'HTTP {status}')
        status, payload = admin.json('POST', '/api/conversations/peer-chat/stop', None,
                                     headers={'x-domain-id': default})
        check('platform_admin', 'admin write bypass absent (stop 404)', status == 404, f'HTTP {status}')
        status, created = admin.json('POST', '/api/conversations', {'title': 'admin walkthrough'},
                                     headers={'x-domain-id': default})
        admin_chat = created.get('id') if status == 201 else None
        check('platform_admin', 'create Admin conversation', status == 201 and admin_chat, f'HTTP {status}')
        status, sent = admin.json('POST', f'/api/conversations/{admin_chat}/messages',
                                  {'text': 'C7 admin walkthrough', 'client_key': 'c7-admin-1'},
                                  headers={'x-domain-id': default})
        check('platform_admin', 'Admin turn accepted', status == 202, f'HTTP {status}')
        for _ in range(200):
            status, snapshot = admin.json('GET', f'/api/conversations/{admin_chat}', headers={'x-domain-id': default})
            if status == 200 and snapshot.get('turns') and snapshot['turns'][0]['status'] in (
                    'succeeded', 'failed', 'cancelled'):
                break
            time.sleep(0.1)
        check('platform_admin', 'Admin turn succeeded on the real backend',
              snapshot['turns'][0]['status'] == 'succeeded', f'status={snapshot["turns"][0]["status"]}')
        status, _, body = admin.request('GET', f'/api/conversations/{admin_chat}/download?path=artifacts/report.txt',
                                        headers={'x-domain-id': default})
        check('platform_admin', 'download produced artifact', status == 200 and b'C7 admin walkthrough' in body,
              f'HTTP {status} bytes={len(body)}')
        status, listed = admin.json('GET', f'/api/conversations?scope=all&domain_id={other}',
                                    headers={'x-domain-id': default})
        check('platform_admin', 'admin can select any enabled domain', status == 200, f'HTTP {status}')
        status, targets = admin.json('GET', '/api/chat/targets', headers={'x-domain-id': other})
        names = [entry.get('name') for entry in targets.get('targets', [])] if status == 200 else []
        check('platform_admin', 'domain selector narrows Agent catalog',
              status == 200 and 'Other Agent' in names and 'Default Agent' not in names, f'{names}')

        # NULL owner was the C4 decision point: 404 for everyone, scope=all included.
        with sqlite3.connect(directory / 'data/runs.sqlite') as data:
            data.execute("UPDATE conversations SET owner_user_id=NULL WHERE id='legacy'")
        status, _ = admin.json('GET', '/api/conversations/legacy?scope=all', headers={'x-domain-id': default})
        check('platform_admin', 'NULL owner is 404 even for scope=all', status == 404, f'HTTP {status}')
        status, _ = developer.json('GET', '/api/conversations/legacy', headers={'x-domain-id': default})
        check('developer', 'NULL owner is 404 for normal users', status == 404, f'HTTP {status}')

        # --- service account: rejected on the page surface --------------------
        service = Client(base)
        status, _ = service.login('service', MEMBER)
        check('service', 'login accepted but page surface denied', status == 200, f'HTTP {status}')
        status, payload = service.json('GET', '/api/chat/targets', headers={'x-domain-id': default})
        check('service', 'targets 403 service_account_forbidden',
              status == 403 and payload.get('code') == 'service_account_forbidden',
              f'HTTP {status} {payload.get("code")}')

        # --- audit: scope=all access and the chat actions are recorded --------
        with sqlite3.connect(directory / 'data/control.sqlite') as control:
            actions = dict(control.execute(
                "SELECT action, COUNT(*) FROM audit_log WHERE level='chat' GROUP BY action").fetchall())
            scope_rows = [row for row in control.execute(
                "SELECT detail FROM audit_log WHERE action='chat.scope_all'").fetchall()]
            send_stop = control.execute(
                "SELECT trace_id FROM audit_log WHERE action='chat.send' LIMIT 1").fetchone()
        check('audit', 'chat actions recorded', {'chat.create', 'chat.send', 'chat.stop', 'chat.download'} <= set(actions),
              f'{actions}')
        check('audit', 'scope=all audited per access', len(scope_rows) >= 3, f'{len(scope_rows)} rows')
        check('audit', 'no message text in audit detail',
              all('C7 walkthrough message' not in row[0] and 'isolation artifact' not in row[0] for row in scope_rows),
              'checked chat.scope_all rows')
        check('audit', 'send carries a turn trace id', bool(send_stop and send_stop[0]),
              f'trace_id={send_stop and send_stop[0]}')
        print('WALKTHROUGH ' + ('PASSED' if not failures else 'FAILED: ' + '; '.join(failures)), flush=True)
        return 0 if not failures else 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
        try:
            subprocess.run([sys.executable, 'tests/e2e/cleanup.py', str(config)], cwd=ROOT, env=environment,
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            if log:
                log.close()


if __name__ == '__main__':
    sys.exit(main())
