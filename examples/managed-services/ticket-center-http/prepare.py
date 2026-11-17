"""Seed the deployment data directory; safely repeatable."""
import json
import sys
from pathlib import Path

SEED_TICKETS = [
    {"id": "T-1001", "title": "支付网关偶发超时", "status": "open", "tags": ["payment", "latency"]},
    {"id": "T-1002", "title": "移动端登录白屏", "status": "open", "tags": ["mobile", "auth"]},
    {"id": "T-1003", "title": "报表导出缺列", "status": "closed", "tags": ["report"]},
    {"id": "T-1004", "title": "健康探测示例工单", "status": "open", "tags": ["probe"]},
]


def main():
    data = Path(sys.argv[1])
    data.mkdir(parents=True, exist_ok=True)
    tickets = data / "tickets.jsonl"
    if not tickets.exists():
        tickets.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in SEED_TICKETS) + "\n",
                           encoding="utf-8")
    print(json.dumps({"ok": True, "tickets": len(tickets.read_text().splitlines())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
