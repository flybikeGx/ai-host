"""Explicit, checkout-local hook installation. Refuse to overwrite another setup."""
import argparse
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('action', choices=['install', 'uninstall'])
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
def git(*args):
    return subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
# Linked worktrees share local config: use worktree scope, never affect siblings.
git('config', '--local', 'extensions.worktreeConfig', 'true').check_returncode()
current = git('config', '--get', 'core.hooksPath').stdout.strip()
target = str(root / '.githooks')
if a.action == 'install':
    if current and current != target:
        raise SystemExit('Existing core.hooksPath; integrate manually, not overwritten: ' + current)
    git('config', '--worktree', 'core.hooksPath', target).check_returncode()
else:
    if current != target:
        raise SystemExit('This checkout does not own the configured hooksPath')
    git('config', '--worktree', '--unset', 'core.hooksPath').check_returncode()
print(a.action + ': ' + target)
