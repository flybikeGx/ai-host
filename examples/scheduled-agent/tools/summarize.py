import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
workspace = Path(request["context"]["workspace"])
activation = json.loads((workspace / "activation.json").read_text())
rows = json.loads((workspace / "input/metrics.json").read_text())
services = {}
for row in rows:
    value = services.setdefault(row["service"], {"requests": 0, "errors": 0})
    value["requests"] += row["requests"]
    value["errors"] += row["errors"]
report = {"marker": "SCHEDULED_AGENT_OK", "trigger_source": activation["source"],
          "trigger_key": activation["key"], "payload": activation["payload"], "services": services,
          "total_requests": sum(v["requests"] for v in services.values()),
          "total_errors": sum(v["errors"] for v in services.values())}
output = workspace / "artifacts/report.json"
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"ok": True, "path": "artifacts/report.json", "report": report}, ensure_ascii=False))
