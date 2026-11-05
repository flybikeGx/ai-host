import json
from pathlib import Path

workspace = Path.cwd()
rows = json.loads((workspace / "input/metrics.json").read_text())
activation = json.loads((workspace / "activation.json").read_text())
report = json.loads((workspace / "artifacts/report.json").read_text())
assert report["marker"] == "SCHEDULED_AGENT_OK"
assert report["total_requests"] == sum(row["requests"] for row in rows)
assert report["total_errors"] == sum(row["errors"] for row in rows)
assert report["trigger_source"] == activation["source"]
assert report["trigger_key"] == activation["key"]
assert report["payload"] == activation["payload"]
expected = {}
for row in rows:
    value = expected.setdefault(row["service"], {"requests": 0, "errors": 0})
    value["requests"] += row["requests"]
    value["errors"] += row["errors"]
assert report["services"] == expected
print("metrics report verified")
