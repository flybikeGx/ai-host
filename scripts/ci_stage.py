"""Own the complete test process tree, including children in detached sessions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil

cancelled = None
owned = {}


def cancel(signum, frame):
    global cancelled
    cancelled = signum


signal.signal(signal.SIGTERM, cancel)
signal.signal(signal.SIGINT, cancel)
child = subprocess.Popen(sys.argv[2:])
root = psutil.Process(child.pid)
try:
    while True:
        try:
            for process in root.children(recursive=True):
                owned[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass
        if cancelled is not None or child.poll() is not None:
            break
        time.sleep(.01)
finally:
    # Process objects validate PID creation time before sending signals.
    processes = [*owned.values(), root]
    for process in reversed(processes):
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    psutil.wait_procs(alive, timeout=2)
    child.wait()
    Path(sys.argv[1]).write_text(json.dumps({'tracked_children': len(owned), 'cancelled_signal': cancelled, 'child_exit_code': child.returncode},indent=2))
sys.exit(128+cancelled if cancelled else child.returncode)
