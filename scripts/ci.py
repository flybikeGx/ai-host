#!/usr/bin/env python3
"""Local CI. No application credentials are inherited by child processes."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['bootstrap', 'quick', 'full', 'contracts'])
    parser.add_argument('--timeout', type=float, default=1800)
    args = parser.parse_args()
    report = ROOT / '.ci-reports' / (time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    report.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT', 'LANG')}
    env.update(CI_REPORT=str(report), PLAYWRIGHT_BROWSERS_PATH=str(ROOT / '.ci-browsers'), PYTHONPATH=str(ROOT / 'src'))
    result = dict(mode=args.mode, system=platform.platform(), architecture=platform.machine(), stages=[], tools={})
    child = None
    spawning = False
    pending_signal = None
    deadline = time.monotonic() + args.timeout

    def interrupted(signum, frame):
        nonlocal pending_signal
        if spawning:
            pending_signal = signum
            return
        raise InterruptedError(str(signum))

    def run(name, command, cwd=ROOT):
        nonlocal child, spawning
        stage = dict(name=name, command=command, exit_code=None)
        result['stages'].append(stage)
        if args.mode != 'bootstrap':
            command = [str(ROOT/'.venv/bin/python'), str(ROOT/'scripts/ci_stage.py'),
                       str(report/(name+'-processes.json')), *command]
        with (report / (name + '.log')).open('w') as log:
            spawning = True
            try:
                child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log, start_new_session=True)
            finally:
                spawning = False
            try:
                if pending_signal is not None:
                    raise InterruptedError(str(pending_signal))
                code = child.wait(timeout=max(.001, deadline-time.monotonic()))
                stage['exit_code'] = code
            finally:
                # A dedicated process group belongs exclusively to this stage.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Darwin can reject signalling an already-exited group.
                    if child.poll() is None:
                        child.kill()
                child.wait()
                if stage['exit_code'] is None:
                    stage['exit_code'] = child.returncode
                child = None
        if code:
            raise subprocess.CalledProcessError(code, command)

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    code = 0
    try:
        for tool, command in {'git':['git','--version'], 'python':[sys.executable,'--version'], 'uv':['uv','--version'], 'node':['node','--version'], 'npm':['npm','--version']}.items():
            if not shutil.which(command[0]):
                raise FileNotFoundError('Missing prerequisite: ' + tool)
            result['tools'][tool] = subprocess.check_output(command, text=True, env=env).strip()
        if sys.version_info < (3, 11):
            raise ValueError('Python >=3.11 required')
        if int(result['tools']['node'].lstrip('v').split('.')[0]) < 22:
            raise ValueError('Node >=22 required')
        if platform.system() == 'Darwin' and int(platform.mac_ver()[0].split('.')[0]) < 13:
            raise ValueError('macOS >=13 required')
        result['commit'] = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
        result['dirty'] = bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True))
        if args.mode == 'bootstrap':
            run('python-install',['uv','sync','--frozen'])
            run('node-install',['npm','ci','--no-audit','--no-fund'],ROOT/'frontend')
            run('browser-install',['npx','--no-install','playwright','install','chromium'],ROOT/'frontend')
        else:
            if not (ROOT/'.venv/bin/python').exists() or not (ROOT/'frontend/node_modules').exists():
                raise FileNotFoundError('Run python3 scripts/ci.py bootstrap first')
            if args.mode == 'contracts':
                # Every frozen contract is already promoted into `full`; this
                # mode audits that the must-pass set still covers C1–C7 and
                # fails on an empty or shrunken suite.
                run('contracts',[str(ROOT/'.venv/bin/python'),'scripts/contracts.py'])
            else:
                run('unit',[str(ROOT/'.venv/bin/python'),'-m','pytest','-q'])
                run('build',['npm','run','build'],ROOT/'frontend')
            if args.mode == 'full':
                # The chat page gate lives in the browser suite; `full` must run
                # it, not leave it to a hand-typed command (AIHO-20 C7).
                run('browser-ui',['npx','--no-install','playwright','test'],ROOT/'frontend')
                run('browser-version',['npx','--no-install','playwright','--version'],ROOT/'frontend')
                run('chromium-version',['node','--input-type=module','-e',
                    "import {chromium} from '@playwright/test'; import {execFileSync} from 'node:child_process'; console.log(execFileSync(chromium.executablePath(), ['--version'], {encoding:'utf8'}));"],ROOT/'frontend')
                run('real-e2e',[str(ROOT/'.venv/bin/python'),'scripts/test_real_e2e.py'])
                run('mock-restart',[str(ROOT/'.venv/bin/python'),'tests/e2e/mock_restart.py'])
        result['status'] = 'passed'
    except subprocess.TimeoutExpired:
        code = 124
        result['status'] = 'timeout'
    except (InterruptedError, KeyboardInterrupt):
        code = 130
        result['status'] = 'cancelled'
    except subprocess.CalledProcessError as exc:
        code = 1
        result.update(status='failed', error=str(exc))
    except (OSError, ValueError) as exc:
        code = 2
        result.update(status='environment_error', error=str(exc))
    finally:
        result['exit_code'] = code
        (report/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(str(report), result['status'], 'exit='+str(code),flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
