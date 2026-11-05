import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
workspace = Path(request["context"]["workspace"])
output = workspace / "artifacts" / "report.txt"
output.parent.mkdir(exist_ok=True)
output.write_text(request["arguments"]["text"] + "\n", encoding="utf-8")
print(json.dumps({"ok": True, "path": "artifacts/report.txt"}))
