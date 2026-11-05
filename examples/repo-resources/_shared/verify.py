import json
import math
from pathlib import Path

workspace = Path.cwd()
report = json.loads((workspace / "artifacts/report.json").read_text())
domain = json.loads((workspace / "domain.json").read_text())
analysis = report["analysis"]
assert analysis["domain"] == domain["name"]
assert analysis["operation"] == domain["operation"]
assert analysis["values"] == [2, 3, 5]
expected = {"sum": 10, "product": 30, "mean": 10 / 3}[domain["operation"]]
assert math.isclose(analysis["value"], expected)
assert analysis["boot_id"] and analysis["pid"] > 0 and analysis["call_number"] > 0
assert report["skill_marker"] == domain["name"].upper() + "_SKILL_OK"
assert report["namespace"].startswith("repo_")
assert report["skill_marker"] in (workspace / ".agent/skills/analysis/SKILL.md").read_text()
print("repo skill + MCP report verified")
