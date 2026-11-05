from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

import psutil


class Cancelled(RuntimeError):
    pass


class DeadlineExceeded(RuntimeError):
    pass


def identity(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def alive(pid: int | None, birth: float | None) -> bool:
    if not pid or birth is None:
        return False
    try:
        proc = psutil.Process(pid)
        return abs(proc.create_time() - birth) < 0.01 and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def kill_tree(pid: int, birth: float | None):
    if not alive(pid, birth):
        return
    if os.name != "nt":
        # Every managed command owns a new session/process group. macOS app
        # sandboxes can forbid global process enumeration (sysctl), so stopping
        # our known group must not depend on psutil.children().
        try:
            group = os.getpgid(pid)
            if group == pid:
                os.killpg(group, signal.SIGTERM)
                time.sleep(0.1)
                try:
                    os.killpg(group, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # Some macOS sandboxes report EPERM rather than ESRCH for
                    # an empty group after SIGTERM has already stopped it.
                    if alive(pid, birth):
                        raise
                return
        except ProcessLookupError:
            return
    else:
        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for proc in reversed(children):
            try:
                proc.terminate()
            except psutil.Error:
                pass
        parent.terminate()
        _, pending = psutil.wait_procs([parent, *children], timeout=1)
        for proc in pending:
            try:
                proc.kill()
            except psutil.Error:
                pass
    except (psutil.Error, OSError):
        pass


def spawn_options() -> dict:
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def platform_command(argv: list[str], env: dict) -> tuple[list[str], dict]:
    if os.name != "nt" or not argv[0].lower().endswith((".cmd", ".bat")):
        return argv, env
    # The task is read from a file/stdin by the native CLI; never interpolated
    # into cmd source. Resolve executable arguments through a fixed PS script.
    import base64

    script = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $a=@($env:AGENT_BASE_ARGV|ConvertFrom-Json); $p=[Console]::In.ReadToEnd(); $p | & $env:AGENT_BASE_EXE @a; exit $LASTEXITCODE"
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode()], {
        **env, "AGENT_BASE_EXE": argv[0], "AGENT_BASE_ARGV": json.dumps(argv[1:])}


def run_process(argv: list[str], *, cwd: Path, env: dict, output: Path, error: Path,
                deadline: float, cancelled: Callable[[], bool], input_file: Path | None = None,
                on_started: Callable[[int, float], None] | None = None, isolate: bool = True,
                on_poll: Callable[[], None] | None = None) -> int:
    if cancelled():
        raise Cancelled("任务已取消")
    if time.time() >= deadline:
        raise DeadlineExceeded("任务超时")
    argv, env = platform_command(argv, env)
    output.parent.mkdir(parents=True, exist_ok=True)
    with (input_file.open("rb") if input_file else open(os.devnull, "rb")) as source, output.open("wb") as out, error.open("wb") as err:
        child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=source, stdout=out, stderr=err, **(spawn_options() if isolate else {}))
        birth = identity(child.pid)
        try:
            if on_started:
                on_started(child.pid, birth)
            while child.poll() is None:
                if on_poll:
                    on_poll()
                if cancelled():
                    raise Cancelled("任务已取消，已停止本次执行器")
                if time.time() >= deadline:
                    raise DeadlineExceeded("任务超时，已停止本次执行器")
                if output.stat().st_size + error.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError("单进程输出超过 32 MiB")
                time.sleep(0.1)
            if output.stat().st_size + error.stat().st_size > 32 * 1024 * 1024:
                raise RuntimeError("单进程输出超过 32 MiB")
            return child.returncode
        finally:
            if child.poll() is None:
                if isolate:
                    kill_tree(child.pid, birth)
                else:
                    child.kill()
                child.wait(timeout=5)
            if isolate and os.name != "nt":
                # A command may exit while its children still hold resources.
                # This is the new process group created for this exact child.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    if child.poll() is None:
                        raise
