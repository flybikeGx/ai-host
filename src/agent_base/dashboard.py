"""Bounded, read-only host/process/queue sampling for the local dashboard."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import platform
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from .repositories import Repositories
from .resource_metrics import WorkspaceSampler, mcp_processes
from .store import Store


BUSY = ('queued', 'preparing', 'running', 'verifying')
FAILURES = ('failed', 'timed_out', 'interrupted')


def pid_state(pid, birth):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or isinstance(birth, bool) or not isinstance(birth, (int, float)) or not math.isfinite(birth):
        return 'missing'
    try:
        proc = psutil.Process(pid)
        return 'alive' if abs(proc.create_time() - birth) < .01 and proc.status() != psutil.STATUS_ZOMBIE else 'missing'
    except psutil.NoSuchProcess:
        return 'missing'
    except (psutil.AccessDenied, OSError):
        return 'unknown'


class ProcessSampler:
    """Each PID/birth is counted once; closest registered root owns a child."""
    def __init__(self):
        self.previous = {}
        self.categories = {}

    def collect(self, roots, cores):
        observed, owners, categories, states, unavailable = {}, {}, {}, {}, 0

        def observe(proc):
            nonlocal unavailable
            if proc.pid in observed:
                return observed[proc.pid]
            try:
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    return None
                with proc.oneshot():
                    born, parent = proc.create_time(), proc.ppid()
                    try:
                        rss = proc.memory_info().rss
                    except (psutil.AccessDenied, OSError):
                        rss = None
                        unavailable += 1
                    try:
                        times = proc.cpu_times()
                        total = times.user + times.system
                    except (psutil.AccessDenied, OSError):
                        total = None
                        unavailable += 1
                mono = time.monotonic()
                key = (proc.pid, born)
                previous = self.previous.get(key)
                cpu = None
                if total is not None and previous and mono > previous[1]:
                    cpu = max(0, (total - previous[0]) / (mono - previous[1]) * 100 / cores)
                observed[proc.pid] = {'pid': proc.pid, 'birth': born, 'ppid': parent,
                                      'rss_bytes': rss, 'cpu_percent': cpu, '_counter': (total, mono)}
                return observed[proc.pid]
            except psutil.NoSuchProcess:
                return None
            except (psutil.AccessDenied, OSError):
                unavailable += 1
                return None

        for root in roots:
            key = (root.get('pid'), root.get('birth'))
            state = pid_state(*key)
            states[key] = state
            if state != 'alive':
                unavailable += int(state == 'unknown')
                continue
            try:
                proc = psutil.Process(root['pid'])
            except psutil.NoSuchProcess:
                states[key] = 'missing'
                continue
            except (psutil.AccessDenied, OSError):
                states[key] = 'unknown'
                unavailable += 1
                continue
            entry = observe(proc)
            if not entry or abs(entry['birth'] - root['birth']) >= .01:
                states[key] = 'unknown' if pid_state(*key) == 'alive' else 'missing'
                continue
            owners[root['pid']] = root['owner']
            categories[root['pid']] = root.get('category', 'agent')
            try:
                children = proc.children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            except (psutil.AccessDenied, OSError):
                unavailable += 1
                children = []
            for child in children:
                observe(child)

        groups, category_groups = {}, {}
        for entry in observed.values():
            pid, visited = entry['pid'], set()
            while pid not in owners and pid in observed and pid not in visited:
                visited.add(pid)
                pid = observed[pid]['ppid']
            owner = owners.get(pid, '_service')
            groups.setdefault(owner, []).append(entry)
            category_groups.setdefault((owner, categories.get(pid, 'service')), []).append(entry)
        self.previous = {(p['pid'], p['birth']): p['_counter'] for p in observed.values() if p['_counter'][0] is not None}

        def aggregate(entries):
            rss = [p['rss_bytes'] for p in entries if p['rss_bytes'] is not None]
            cpu = [p['cpu_percent'] for p in entries if p['cpu_percent'] is not None]
            return {'process_count': len(entries), 'rss_bytes': sum(rss) if rss else (0 if not entries else None),
                    'cpu_percent': round(sum(cpu), 2) if cpu else (0 if not entries else None),
                    'rss_complete': len(rss) == len(entries), 'cpu_complete': len(cpu) == len(entries)}
        metrics = {owner: aggregate(entries) for owner, entries in groups.items()}
        self.categories = {key: aggregate(entries) for key, entries in category_groups.items()}
        total = {**aggregate(list(observed.values())), 'unreadable': unavailable}
        if unavailable:
            for values in [total, *metrics.values(), *self.categories.values()]:
                values['rss_complete'] = values['cpu_complete'] = False
        return metrics, total, states


class Dashboard:
    interval = 3
    history_limit = 120

    def __init__(self, config, scheduler, *, scheduler_thread=None):
        self.config, self.scheduler = config, scheduler
        self.scheduler_thread = scheduler_thread
        self.store, self.repositories = Store(config.data), Repositories(config)
        self.created = time.time()
        self.stop_event, self.thread = threading.Event(), None
        self.sample_lock, self.data_lock = threading.Lock(), threading.Lock()
        self.data, self.last_error = None, False
        self.history = deque(maxlen=self.history_limit)
        self.processes = ProcessSampler()
        self.workspaces = WorkspaceSampler(config.data)
        self.host_previous, self.health_cache = None, {}

    def start(self):
        self.thread = threading.Thread(target=self.run, name='dashboard-sampler', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.is_set():
            self.sample()
            self.stop_event.wait(self.interval)

    def snapshot(self):
        with self.data_lock:
            empty = self.data is None
            old = self.data and time.time() - self.data['sampled_at'] >= self.interval
        if empty or (old and (not self.thread or not self.thread.is_alive())):
            self.sample()
        with self.data_lock:
            value = copy.deepcopy(self.data)
            error = self.last_error
        if value is None:
            return {'available': False, 'sampling_error': error, 'sampled_at': None, 'history': []}
        value.update(available=True, age_seconds=round(max(0, time.time() - value['sampled_at']), 1), sampling_error=error)
        value['stale'] = value['age_seconds'] > 12
        return value

    def sample(self):
        with self.sample_lock:
            started = time.monotonic()
            try:
                value = self._sample()
                value['duration_ms'] = round((time.monotonic() - started) * 1000)
                self.history.append({'at': value['sampled_at'], 'host_cpu_percent': value['host']['cpu_percent'],
                                     'managed_cpu_percent': value['managed']['cpu_percent'],
                                     'memory_percent': value['host']['memory']['percent']})
                value['history'] = list(self.history)
                with self.data_lock:
                    self.data, self.last_error = value, False
            except Exception:
                logging.exception('Dashboard sample failed; preserving previous snapshot')
                with self.data_lock:
                    self.last_error = True

    def _probe(self, item):
        result = {'checked_at': time.time(), 'healthy': False, 'upstreams': {}, 'processes': {}, 'latency_ms': None}
        started = time.monotonic()
        try:
            endpoint = urlsplit(item.get('endpoint') or '')
            if endpoint.scheme != 'http' or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}:
                raise ValueError('not a managed loopback endpoint')
            with httpx.Client(timeout=.7, trust_env=False) as client:
                response = client.get(item['endpoint'].removesuffix('/mcp') + '/health', headers=self.repositories.headers(item))
                response.raise_for_status()
                health = response.json()
            if health.get('installation_id') != item['id']:
                raise ValueError('installation mismatch')
            result.update(healthy=health.get('healthy') is True,
                          upstreams={str(k): 'ready' if v == 'ready' else 'unavailable' for k, v in health.get('upstreams', {}).items()},
                          processes={str(k): {'pid': v.get('pid'), 'birth': v.get('birth')}
                                     for k, v in health.get('processes', {}).items() if isinstance(v, dict)},
                          latency_ms=round((time.monotonic() - started) * 1000, 1))
        except (OSError, ValueError, httpx.HTTPError):
            pass
        return result

    def _sample(self):
        now, cores = time.time(), psutil.cpu_count() or 1
        cpu = psutil.cpu_times()
        total = sum(cpu) - getattr(cpu, 'guest', 0) - getattr(cpu, 'guest_nice', 0)
        busy = total - cpu.idle - getattr(cpu, 'iowait', 0)
        host_cpu = None
        if self.host_previous and total > self.host_previous[0]:
            host_cpu = round(min(100, max(0, (busy - self.host_previous[1]) / (total - self.host_previous[0]) * 100)), 1)
        self.host_previous = (total, busy)
        memory, disk = psutil.virtual_memory(), psutil.disk_usage(str(self.config.data))
        memory_percent = round((memory.total - memory.available) / memory.total * 100, 1)
        business = 'NOT EXISTS (SELECT 1 FROM conversation_turns ct WHERE ct.run_id=r.id)'
        with self.store.connection() as conn:
            conn.execute('BEGIN')
            installations = [dict(r) for r in conn.execute('SELECT * FROM repo_installations ORDER BY created DESC')]
            runs = [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status IN ('queued','preparing','running','verifying')")]
            turns = [dict(r) for r in conn.execute("SELECT t.*,c.target FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.status IN ('queued','running')")]
            saved = dict(conn.execute('SELECT target,count(*) FROM conversations GROUP BY target').fetchall())
            counts = dict(conn.execute(f'SELECT status,count(*) FROM runs r WHERE {business} GROUP BY status').fetchall())
            recent_counts = dict(conn.execute(f"SELECT status,count(*) FROM runs r WHERE {business} AND status IN ('succeeded','failed','timed_out','interrupted','cancelled') AND COALESCE(finished_at,updated)>=? GROUP BY status", (now-86400,)).fetchall())
            failures = [dict(r) for r in conn.execute(f"SELECT id,status,updated,finished_at,spec FROM runs r WHERE {business} AND status IN ('failed','timed_out','interrupted') ORDER BY COALESCE(finished_at,updated) DESC LIMIT 5")]
            chat_failures = [dict(r) for r in conn.execute("SELECT t.id,t.status,t.updated,c.id AS conversation_id,c.target FROM conversation_turns t JOIN conversations c ON c.id=t.conversation_id WHERE t.status IN ('failed','timed_out','interrupted') ORDER BY t.updated DESC LIMIT 5")]
            works = [dict(r) for r in conn.execute('SELECT * FROM works WHERE enabled=1 AND next_due IS NOT NULL ORDER BY next_due LIMIT 5')]
            disk_runs = [dict(r) for r in conn.execute(f'SELECT id,status,workspace,spec FROM runs r WHERE workspace IS NOT NULL AND {business} ORDER BY updated DESC LIMIT 201')]
            disk_chats = [dict(r) for r in conn.execute('SELECT id,target FROM conversations ORDER BY updated DESC LIMIT 201')]

        agents = {'admin': {'id': 'admin', 'name': 'Admin Agent', 'kind': 'admin', 'commit': None, 'installed': True}}
        roots = [{'pid': os.getpid(), 'birth': psutil.Process().create_time(), 'owner': '_service', 'category': 'service'}]
        due = []
        for item in installations:
            item['manifest'] = json.loads(item['manifest'])
            agents[item['id']] = {'id': item['id'], 'name': item['manifest'].get('package', {}).get('name', item['repo_id']),
                                  'kind': 'repo', 'commit': item['commit_sha'][:7], 'installed': item['status'] == 'ready'}
            identity = (item.get('pid'), item.get('birth'), item.get('endpoint'))
            cached = self.health_cache.get(item['id'])
            item['_pid_state'] = pid_state(item.get('pid'), item.get('birth'))
            if item['enabled'] and item['status'] == 'ready' and item['_pid_state'] != 'missing':
                if not cached or cached[0] != identity or now - cached[1]['checked_at'] >= 10:
                    due.append(item)
            if item['_pid_state'] == 'alive':
                roots.append({'pid': item['pid'], 'birth': item['birth'], 'owner': 'gateway-' + item['id'], 'category': 'gateway'})
        due.sort(key=lambda item: self.health_cache.get(item['id'], (None, {'checked_at': 0}))[1]['checked_at'])
        if due:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for item, health in zip(due[:8], pool.map(self._probe, due[:8])):
                    self.health_cache[item['id']] = ((item.get('pid'), item.get('birth'), item.get('endpoint')), health)
        self.health_cache = {k: v for k, v in self.health_cache.items() if k in agents}

        def agent_for(spec):
            if spec.get('installation_id'):
                key = spec['installation_id']
            else:
                origin = spec.get('repo', 'unknown')
                key = 'legacy-' + hashlib.sha256(origin.encode()).hexdigest()[:12]
                agents.setdefault(key, {'id': key, 'name': Path(origin.rstrip('/')).name.removesuffix('.git')[:80],
                                        'kind': 'legacy', 'commit': None, 'installed': False})
            agents.setdefault(key, {'id': key, 'name': 'Agent', 'kind': 'repo', 'commit': None, 'installed': False})
            return key

        candidates, logical = [], {}
        for job in runs:
            spec = json.loads(job['spec'])
            owner = agent_for(spec)
            if job['pid']:
                roots.append({'pid': job['pid'], 'birth': job['birth'], 'owner': '_service'})
            if not spec.get('conversation_id') and job['process_pid'] and job['status'] == 'running' and spec.get('runtime', {}).get('engine', 'dsh') == 'dsh':
                candidates.append({'id': job['id'], 'kind': 'run', 'agent_id': owner, 'pid': job['process_pid'], 'birth': job['process_birth'],
                                   'workspace_key': 'run-' + job['id']})
            if not spec.get('conversation_id'):
                logical.setdefault(owner, Counter())[job['status']] += 1
        for turn in turns:
            owner = turn['target']
            agents.setdefault(owner, {'id': owner, 'name': 'Agent', 'kind': 'repo', 'commit': None, 'installed': False})
            logical.setdefault(owner, Counter())['turn_' + turn['status']] += 1
            if turn['pid']:
                roots.append({'pid': turn['pid'], 'birth': turn['birth'], 'owner': '_service'})
            if turn['child_pid']:
                candidates.append({'id': turn['id'], 'kind': 'conversation', 'agent_id': owner, 'pid': turn['child_pid'], 'birth': turn['child_birth'],
                                   'conversation_id': turn['conversation_id'], 'workspace_key': 'conversation-' + turn['conversation_id']})
        for file in (self.config.data / 'admin').glob('*/process.json'):
            try:
                if file.stat().st_size > 16384:
                    continue
                item = json.loads(file.read_text(encoding='utf-8'))
                if pid_state(item.get('pid'), item.get('birth')) == 'missing':
                    continue
                if item.get('supervisor_pid'):
                    roots.append({'pid': item['supervisor_pid'], 'birth': item.get('supervisor_birth'), 'owner': '_service'})
                candidates.append({'id': file.parent.name, 'kind': 'admin_cli', 'agent_id': 'admin', 'pid': item['pid'], 'birth': item['birth'],
                                   'workspace_key': 'admin-' + file.parent.name})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for item in candidates:
            item['resource_key'] = 'instance-' + item['kind'] + '-' + item['id']
            roots.append({'pid': item['pid'], 'birth': item['birth'], 'owner': item['resource_key'], 'category': 'dsh'})
        mcp_identities = {}
        for item in installations:
            cached = self.health_cache.get(item['id'])
            health = cached[1] if cached and cached[0] == (item.get('pid'), item.get('birth'), item.get('endpoint')) else {}
            identities = mcp_processes(item, health.get('processes', {})) if item['_pid_state'] == 'alive' else {}
            mcp_identities[item['id']] = identities
            for server_id, identity in identities.items():
                roots.append({**identity, 'owner': 'mcp-' + item['id'] + '-' + server_id, 'category': 'mcp'})
        group_metrics, managed, process_states = self.processes.collect(roots, cores)
        instances, seen = [], set()
        for item in candidates:
            identity = (item['pid'], item['birth'])
            state = process_states.get(identity, 'missing')
            if identity not in seen and state in {'alive', 'unknown'}:
                instances.append({**{k: v for k, v in item.items() if k != 'resource_key'}, 'state': state,
                                  'engine': 'dsh', 'agent_name': agents[item['agent_id']]['name'],
                                  'resources': group_metrics.get(item['resource_key'])})
                seen.add(identity)

        gateways, mcps = [], []
        for item in installations:
            cached = self.health_cache.get(item['id'])
            health = cached[1] if cached and cached[0] == (item.get('pid'), item.get('birth'), item.get('endpoint')) else None
            if not item['enabled']:
                state = 'disabled'
            elif item['status'] != 'ready':
                state = item['status']
            elif item['_pid_state'] == 'missing':
                state = 'down'
            elif not health or now - health['checked_at'] > 30:
                state = 'checking'
            elif health['healthy']:
                state = 'healthy'
            else:
                state = 'unhealthy'
            for specification in item['manifest'].get('mcp', {}).get('servers', []):
                server_id = specification['id']
                identity = mcp_identities[item['id']].get(server_id)
                mcps.append({'id': item['id'] + ':' + server_id, 'server_id': server_id, 'installation_id': item['id'],
                             'agent_name': agents[item['id']]['name'], 'transport': specification['transport'],
                             'state': health['upstreams'].get(server_id, 'unknown') if health and state in {'healthy', 'unhealthy'} else state,
                             'pid': identity['pid'] if identity else None,
                             'process_state': pid_state(identity['pid'], identity['birth']) if identity else 'unknown',
                             'resources': group_metrics.get('mcp-' + item['id'] + '-' + server_id),
                             'metrics_state': 'remote' if specification['transport'] != 'stdio' else 'measured' if identity else 'unattributed',
                             'checked_at': health['checked_at'] if health else None})
            gateways.append({'installation_id': item['id'], 'name': agents[item['id']]['name'], 'namespace': 'repo_' + item['repo_id'],
                         'commit': item['commit_sha'][:7], 'state': state, 'process_state': item['_pid_state'],
                         'pid': item['pid'], 'checked_at': health['checked_at'] if health else None,
                         'latency_ms': health['latency_ms'] if health else None,
                         'mcp_count': len(item['manifest'].get('mcp', {}).get('servers', [])),
                         'restart_attempts': item['restart_attempts'], 'next_retry': item['next_retry'],
                         'resources': group_metrics.get('gateway-' + item['id'])})

        recent = []
        for item in failures:
            spec = json.loads(item['spec'])
            owner = agent_for(spec)
            recent.append({'id': item['id'], 'kind': 'run', 'status': item['status'], 'at': item['finished_at'] or item['updated'], 'agent_name': agents[owner]['name']})
        for item in chat_failures:
            recent.append({'id': item['id'], 'kind': 'conversation', 'status': item['status'], 'at': item['updated'],
                           'conversation_id': item['conversation_id'], 'agent_name': agents.get(item['target'], {}).get('name', 'Agent')})
        recent.sort(key=lambda item: item['at'], reverse=True)
        for key, agent in agents.items():
            activity = logical.get(key, Counter())
            agent.update(running_instances=sum(i['agent_id'] == key and i['state'] == 'alive' for i in instances),
                         queued=activity['queued'] + activity['turn_queued'],
                         active_runs=sum(activity[s] for s in ('preparing', 'running', 'verifying')),
                         active_turns=activity['turn_running'] + sum(i['agent_id']==key and i['kind']=='admin_cli' and i['state']=='alive' for i in instances), conversations=saved.get(key, 0))

        workspace_rows = {}
        active_keys = {i['workspace_key'] for i in instances if i['state'] == 'alive'}
        def add_workspace(key, owner, path, kind, status, record_id):
            if not path:
                return
            path = Path(path)
            try:
                label = path.relative_to(self.config.data).as_posix()
            except ValueError:
                label = '外部目录（不扫描）'
            workspace_rows[key] = {'id': key, 'record_id': record_id, 'kind': kind, 'agent_name': agents.get(owner, {}).get('name', 'Agent'),
                                   'active': key in active_keys, 'status': status, 'label': label, '_path': path}
        # Active instances always get a disk measurement, even beyond the recent
        # retained-workspace inventory limit.
        for job in [*disk_runs[:200], *runs]:
            if json.loads(job['spec']).get('conversation_id'):
                continue
            add_workspace('run-' + job['id'], agent_for(json.loads(job['spec'])), job['workspace'], 'run', job['status'], job['id'])
        for chat in disk_chats[:200]:
            add_workspace('conversation-' + chat['id'], chat['target'], self.config.data / 'conversations' / chat['id'] / 'workspace', 'conversation', 'retained', chat['id'])
        for turn in turns:
            add_workspace('conversation-' + turn['conversation_id'], turn['target'], self.config.data / 'conversations' / turn['conversation_id'] / 'workspace', 'conversation', turn['status'], turn['conversation_id'])
        for path in list((self.config.data / 'admin').glob('*/workspace'))[:200]:
            add_workspace('admin-' + path.parent.name, 'admin', path, 'admin_cli', 'retained', path.parent.name)
        measurements = self.workspaces.collect([row['_path'] for row in sorted(workspace_rows.values(), key=lambda r: not r['active'])])
        for row in workspace_rows.values():
            row['size'] = measurements[row.pop('_path')]
        for instance in instances:
            workspace = workspace_rows.get(instance['workspace_key'])
            instance['workspace'] = workspace
        workspace_list = [r for r in workspace_rows.values() if r['size']['state'] != 'missing']
        workspace_list.sort(key=lambda r: (r['active'], r['size']['logical_bytes'] or 0), reverse=True)
        workspace_stats = {'count': len(workspace_list), 'active_count': sum(r['active'] for r in workspace_list),
                           'logical_bytes': sum(r['size']['logical_bytes'] or 0 for r in workspace_list),
                           'complete': all(r['size']['complete'] for r in workspace_list),
                           'inventory_limited': len(disk_runs) > 200 or len(disk_chats) > 200,
                           'sample_interval_seconds': self.workspaces.ttl}

        plans = [{'id': w['id'], 'next_due': w['next_due'], 'title': json.loads(w['definition']).get('schedule', {}).get('title', w['id'])} for w in works]
        running_thread = bool(self.scheduler_thread and self.scheduler_thread.is_alive() and not self.scheduler.stop_event.is_set())
        tick = self.scheduler.last_tick_at
        scheduler_state = 'stopped' if self.scheduler_thread and not running_thread else 'disabled' if not running_thread else 'delayed' if now - (tick or self.created) > 15 else 'healthy' if tick else 'starting'
        scheduler_error = self.scheduler.last_error_at and (not tick or self.scheduler.last_error_at > tick)
        if running_thread and scheduler_error:
            scheduler_state = 'error'
        alerts = []
        if memory_percent >= 90:
            alerts.append({'kind': 'memory', 'text': '本机内存占用超过 90%'})
        if disk.percent >= 90:
            alerts.append({'kind': 'disk', 'text': '状态数据所在磁盘占用超过 90%'})
        if host_cpu is not None and host_cpu >= 90:
            alerts.append({'kind': 'cpu', 'text': '本次采样 CPU 占用超过 90%'})
        bad = [m for m in gateways if m['state'] in {'down', 'unhealthy', 'failed'}]
        if bad:
            alerts.append({'kind': 'mcp', 'text': f'{len(bad)} 个 Repo 资源服务异常'})
        if scheduler_state in {'delayed', 'stopped'} or scheduler_error:
            alerts.append({'kind': 'scheduler', 'text': '调度循环延迟或最近一次循环出错'})
        db_size = 0
        for file in (self.store.db, Path(str(self.store.db)+'-wal'), Path(str(self.store.db)+'-shm')):
            try:
                db_size += file.stat().st_size
            except OSError:
                pass
        return {'sampled_at': time.time(), 'interval_seconds': self.interval, 'uptime_seconds': int(now-self.created),
                'host': {'platform': platform.system(), 'logical_cores': cores, 'cpu_percent': host_cpu,
                         'memory': {'total_bytes': memory.total, 'available_bytes': memory.available, 'used_bytes': memory.total-memory.available, 'percent': memory_percent},
                         'disk': {'total_bytes': disk.total, 'used_bytes': disk.used, 'free_bytes': disk.free, 'percent': disk.percent}},
                'managed': managed, 'agents': list(agents.values()), 'instances': instances, 'mcp': mcps, 'gateways': gateways,
                'workspaces': workspace_list[:100], 'workspace_stats': workspace_stats,
                'counts': {'installed_agents': len({i['repo_id'] for i in installations if i['status']=='ready'}),
                           'running_instances': sum(i['state']=='alive' for i in instances), 'unknown_instances': sum(i['state']=='unknown' for i in instances),
                           'queued_runs': counts.get('queued', 0), 'queued_turns': sum(t['status']=='queued' for t in turns),
                           'active_runs': sum(counts.get(s, 0) for s in ('preparing','running','verifying')),
                           'active_turns': sum(t['status']=='running' for t in turns) + sum(i['kind']=='admin_cli' and i['state']=='alive' for i in instances), 'saved_conversations': sum(saved.values()),
                           'occupied_slots': sum(r['status'] in ('preparing','running','verifying') for r in runs), 'max_concurrent': self.config.max_concurrent,
                           'healthy_mcp': sum(m['state']=='ready' for m in mcps),
                           'declared_mcp': len(mcps), 'healthy_gateways': sum(m['state']=='healthy' for m in gateways)},
                'last_24h': recent_counts, 'recent_failures': recent[:8], 'alerts': alerts, 'next_plans': plans,
                'scheduler': {'state': scheduler_state, 'last_tick_at': tick, 'last_error_at': self.scheduler.last_error_at},
                'database_bytes': db_size}
