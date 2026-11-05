import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
workspace = Path(request["context"]["workspace"])
report = {**request["arguments"], "namespace": request["context"]["namespace"]}
target = workspace / "artifacts/report.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({"ok": True, "path": "artifacts/report.json"}))
