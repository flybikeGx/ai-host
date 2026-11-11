"""Read-only resource attribution and bounded workspace size sampling."""
from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path

import psutil

from .package import expand


def direct_children(pid):
    """Identity snapshots also work for SDK versions that don't expose a PID."""
    try:
        return {(p.pid, p.create_time()) for p in psutil.Process(pid).children()}
    except (psutil.Error, OSError):
        return set()


def mcp_processes(item, reported):
    """Map a stdio service to one verified direct child; never divide a total."""
    children = direct_children(item['pid'])
    result, matches = {}, {}
    specifications = item['manifest'].get('mcp', {}).get('servers', [])
    for specification in specifications:
        if specification['transport'] != 'stdio':
            continue
        server_id = specification['id']
        identity = reported.get(server_id, {})
        key = (identity.get('pid'), identity.get('birth'))
        if isinstance(key[0], int) and isinstance(key[1], (int, float)) and key in children:
            matches[server_id] = [key]
            continue
        # Old running gateways have no identity metadata. Match their *actual*
        # direct child argv without exposing it. Ambiguous/wrapped commands stay
        # unknown, so updating the dashboard never requires restarting an MCP.
        try:
            package = Path(item['root']) / 'source'
            python = (Path(item['root']) / 'python.txt').read_text().strip()
            argv = expand(specification['argv'], package, package, python)

            def executable(value):
                return os.path.normcase(str(Path(shutil.which(value) or value).resolve()))

            candidates = []
            for pid, birth in children:
                try:
                    proc = psutil.Process(pid)
                    if proc.create_time() != birth:
                        continue
                    actual = proc.cmdline()
                except (psutil.Error, OSError):
                    continue
                if actual and actual[1:] == argv[1:] and executable(actual[0]) == executable(argv[0]):
                    candidates.append((pid, birth))
            matches[server_id] = candidates
        except (psutil.Error, OSError, ValueError):
            matches[server_id] = []
    # Also reject two indistinguishable declarations matching the same child.
    for server_id, candidates in matches.items():
        if len(candidates) == 1 and sum(candidates[0] in values for values in matches.values()) == 1:
            result[server_id] = {'pid': candidates[0][0], 'birth': candidates[0][1]}
    return result


class WorkspaceSampler:
    ttl = 30
    max_entries = 20000
    scan_seconds = .1
    scans_per_tick = 8

    def __init__(self, data):
        self.data = data.resolve()
        self.cache = {}

    def measure(self, path):
        now = time.time()
        result = {'logical_bytes': 0, 'allocated_bytes': 0, 'files': 0,
                  'complete': True, 'state': 'ready', 'checked_at': now}
        try:
            # Only registered workspaces inside managed state; external skill
            # links are ignored and shared templates are not charged per run.
            if path.is_symlink() or not path.resolve().is_relative_to(self.data):
                return {**result, 'logical_bytes': None, 'allocated_bytes': None, 'complete': False, 'state': 'outside'}
            if not path.is_dir():
                return {**result, 'state': 'missing'}
            until, stack, seen, entries = time.monotonic() + self.scan_seconds, [path], set(), 0
            while stack:
                directory = stack.pop()
                try:
                    with os.scandir(directory) as listing:
                        for entry in listing:
                            entries += 1
                            if entries > self.max_entries or time.monotonic() > until:
                                return {**result, 'complete': False, 'state': 'partial'}
                            try:
                                info = entry.stat(follow_symlinks=False)
                                if stat.S_ISLNK(info.st_mode):
                                    continue
                                if stat.S_ISDIR(info.st_mode):
                                    stack.append(Path(entry.path))
                                elif stat.S_ISREG(info.st_mode):
                                    key = (info.st_dev, info.st_ino)
                                    if key in seen:
                                        continue
                                    seen.add(key)
                                    result['files'] += 1
                                    result['logical_bytes'] += info.st_size
                                    if result['allocated_bytes'] is not None:
                                        if hasattr(info, 'st_blocks'):
                                            result['allocated_bytes'] += info.st_blocks * 512
                                        else:
                                            result['allocated_bytes'] = None
                            except OSError:
                                result.update(complete=False, state='partial')
                except OSError:
                    result.update(complete=False, state='partial')
        except OSError:
            result.update(logical_bytes=None, allocated_bytes=None, complete=False, state='unavailable')
        return result

    def collect(self, paths):
        paths = list(dict.fromkeys(Path(p) for p in paths))
        allowed = set(paths)
        self.cache = {p: value for p, value in self.cache.items() if p in allowed}
        now = time.time()
        due = sorted((p for p in paths if p not in self.cache or now-self.cache[p]['checked_at'] >= self.ttl
                      or (self.cache[p]['state']=='missing' and p.is_dir())),
                     key=lambda p: self.cache.get(p, {}).get('checked_at', 0))
        for path in due[:self.scans_per_tick]:
            self.cache[path] = self.measure(path)
        pending = {'logical_bytes': None, 'allocated_bytes': None, 'files': None,
                   'complete': False, 'state': 'pending', 'checked_at': None}
        return {p: self.cache.get(p, pending) for p in paths}
