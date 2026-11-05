from __future__ import annotations

import json
import os
import shutil
import signal
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Config, PROJECT
from .engines import dsh_command, dsh_patch, parse_dsh_output, runtime_environment
from .processes import Cancelled, run_process
from .store import Store


@contextmanager
def chat_lock(path: Path):
    with path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.write(b"0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("同名 admin 会话已打开，请关闭另一窗口或使用 --session 指定不同会话") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class Admin:
    def __init__(self, config: Config, session: str = "default"):
        if not session or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in session):
            raise ValueError("admin session 只支持字母、数字、下划线和短横线")
        if config.runtime.get("engine", "dsh") != "dsh":
            raise ValueError("admin 对话目前使用 dsh；CodeAgent 的管理 MCP 入口需在内网适配后启用")
        self.config, self.name = config, session
        self.store = Store(config.data)
        self.root = config.data / "admin" / session
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(exist_ok=True)
        # Bound instruction discovery to this dedicated workspace.
        (self.workspace / ".agent-base-root").touch()
        shutil.copyfile(PROJECT / "AGENTS.md", self.workspace / "AGENTS.md")

    def turn(self, text: str) -> str:
        if not text.strip():
            return ""
        with chat_lock(self.root / "chat.lock"):
            previous = self.store.chat_id(self.name)
            turn_root = self.root / ("turn-" + str(time.time_ns()))
            turn_root.mkdir()
            patch = dsh_patch(self.config.runtime, turn_root, admin_config=self.config.path)
            task = turn_root / "prompt.txt"
            task.write_text((PROJECT / "AGENTS.md").read_text(encoding="utf-8") + "\n\n用户本轮请求：\n" + text, encoding="utf-8")
            stopped = [False]
            old = signal.signal(signal.SIGINT, lambda *_: stopped.__setitem__(0, True))
            try:
                code = run_process(dsh_command(self.config.runtime, patch, session_id=previous), cwd=self.workspace,
                    env=runtime_environment(self.config.runtime, self.root / "dsh-home"),
                    input_file=task, output=turn_root / "runtime.jsonl", error=turn_root / "runtime.stderr.log",
                    deadline=time.time() + self.config.runtime.get("timeout_seconds", 1800), cancelled=lambda: stopped[0])
                # Persist any established native identity even if the activity
                # failed after submitting a task; later chat must not forget it.
                for line in (turn_root / "runtime.jsonl").read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                        if event.get("type") == "session" and event.get("sessionId"):
                            self.store.save_chat_id(self.name, event["sessionId"])
                    except ValueError:
                        pass
                if code:
                    raise RuntimeError(f"admin dsh 退出码 {code}；查看 {turn_root / 'runtime.stderr.log'}。已提交任务不会自动重发。")
                result = parse_dsh_output(turn_root / "runtime.jsonl")
                self.store.save_chat_id(self.name, result["session_id"])
                return result["text"]
            finally:
                signal.signal(signal.SIGINT, old)

    def chat(self):
        print("Admin Agent 已启动。输入仓库地址和任务即可；/exit 退出。后台任务在退出对话后继续运行。")
        while True:
            try:
                text = input("你> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if text.strip() in {"/exit", "/quit"}:
                return
            try:
                response = self.turn(text)
                if response:
                    print("admin> " + response)
            except (ValueError, RuntimeError) as error:
                print("admin> " + str(error))
