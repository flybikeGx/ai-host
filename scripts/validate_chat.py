"""Live HTTP + native dsh multi-turn chat acceptance. Uses configured credentials."""
import json
import os
import time
import uuid
from pathlib import Path

import httpx

from agent_base.config import Config
from agent_base.repositories import Repositories

config = Config.load()
headers = {"Authorization": "Bearer " + os.environ["AGENT_BASE_WEB_TOKEN"]} if os.environ.get("AGENT_BASE_WEB_TOKEN") else {}
client = httpx.Client(base_url="http://127.0.0.1:8765", headers=headers, trust_env=False, timeout=30)
repositories = Repositories(config)


def call(method, path, body=None):
    response = client.request(method, "/api/" + path, json=body)
    response.raise_for_status()
    return response.json()


def send(chat, text, key=None):
    return call("POST", f"conversations/{chat['id']}/messages", {"text": text, "client_key": key or uuid.uuid4().hex})


def wait(chat, turn):
    until = time.time() + 180
    previous = None
    while time.time() < until:
        snapshot = call("GET", "conversations/" + chat["id"])
        current = next(t for t in snapshot["turns"] if t["id"] == turn["id"])
        if current["status"] != previous:
            print(json.dumps({"chat": chat["title"], "turn": turn["id"], "status": current["status"]}), flush=True)
            previous = current["status"]
        if current["status"] not in {"queued", "running"}:
            assert current["status"] == "succeeded", current
            return snapshot, current
        time.sleep(0.3)
    raise AssertionError("chat timeout")


try:
    installed = {item["manifest"]["package"]["name"].removesuffix("-agent"): item for item in repositories.list() if item["status"] == "ready"}
    assert {"finance", "inventory", "research"} <= set(installed)
    before = {name: repositories.health(item) for name, item in installed.items()}
    memory = call("POST", "conversations", {"target": "admin", "title": "上下文续接验证"})
    marker = "CHAT_MEMORY_" + uuid.uuid4().hex[:12]
    first = send(memory, f"请在本会话记住标记 {marker}，只回复‘记住了’，不要调用工具或写文件。", "remember")
    _, answer = wait(memory, first)
    remembered = call("GET", "conversations/" + memory["id"])["conversation"]["native_id"]
    second = send(memory, "我上一条消息给出的会话标记是什么？只回复标记，不调用工具、不读写文件。", "recall")
    replayed = send(memory, "我上一条消息给出的会话标记是什么？只回复标记，不调用工具、不读写文件。", "recall")
    assert replayed["duplicate"] and replayed["id"] == second["id"]
    snapshot, answer = wait(memory, second)
    assert marker in answer["assistant_text"], answer
    assert snapshot["conversation"]["native_id"] == remembered
    assert len(snapshot["turns"]) == 2
    assert not any(e["type"] == "tool.call" for e in snapshot["events"])
    call("PATCH", "conversations/" + memory["id"], {"archived": True})

    admin = call("POST", "conversations", {"target": "admin", "title": "管理我的 Agent"})
    admin_turn = send(admin, "调用管理工具列出已经安装的 Agent、各自的 skill 和 MCP 服务状态，用中文简要介绍，不启动新的工作。")
    # Attach and deliberately disconnect the viewer while generation continues.
    with client.stream("GET", "/api/conversations/" + admin["id"] + "/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: ") and json.loads(line[6:])["type"] == "session.opened":
                break
    admin_snapshot, _ = wait(admin, admin_turn)
    assert any(e["type"] == "tool.call" and "admin" in e["data"]["tool"] for e in admin_snapshot["events"])

    conversations = {}
    turns = {}
    for name, title in (("finance", "财务分析"), ("inventory", "库存分析"), ("research", "研究分析")):
        chat = call("POST", "conversations", {"target": installed[name]["id"], "title": title})
        conversations[name] = chat
        turns[name] = send(chat, "请使用你自己的 read_skill 读取 analysis，调用 domain__analyze 分析 [2, 3, 5]，按技能使用 write_report 保存结果。")
    results = {}
    for name in conversations:
        snapshot, answer = wait(conversations[name], turns[name])
        assert snapshot["conversation"]["native_id"]
        tool_names = [e["data"]["tool"] for e in snapshot["events"] if e["type"] == "tool.call"]
        assert any(installed[name]["namespace"] in tool for tool in tool_names)
        for other in installed:
            if other != name:
                assert not any(installed[other]["namespace"] in tool for tool in tool_names)
        files = call("GET", "conversations/" + conversations[name]["id"] + "/files")
        assert any(f["path"] == "artifacts/report.json" for f in files)
        response = client.get("/api/conversations/" + conversations[name]["id"] + "/download", params={"path": "artifacts/report.json"})
        response.raise_for_status()
        report = response.json()
        assert report["analysis"]["domain"] == name
        results[name] = snapshot
    finance_session = results["finance"]["conversation"]["native_id"]
    follow = send(conversations["finance"], "刚才这份分析的数值结果是多少？仅根据本会话简短回答，不再调用工具或重生成报告。")
    finance, answer = wait(conversations["finance"], follow)
    assert "10" in answer["assistant_text"]
    assert finance["conversation"]["native_id"] == finance_session
    assert len(finance["turns"]) == 2
    results["finance"] = finance
    for name, item in installed.items():
        assert repositories.health(repositories.get(item["id"]))["boot_id"] == before[name]["boot_id"]
    saved = config.data / "validations" / ("chat-" + str(time.time_ns()))
    saved.mkdir(parents=True)
    (saved / "result.json").write_text(json.dumps({"kind": "live web API + native dsh conversations", "admin": admin_snapshot,
        "repo_chats": results, "native_memory_verified": True, "duplicate_request_verified": True,
        "disconnect_did_not_cancel": True, "mcp_services_unchanged": True}, ensure_ascii=False, indent=2))
    print(json.dumps({"validation": "passed", "receipt": str(saved / "result.json"),
                      "finance_url": "http://127.0.0.1:8765/chat#" + conversations["finance"]["id"]}), flush=True)
finally:
    client.close()
