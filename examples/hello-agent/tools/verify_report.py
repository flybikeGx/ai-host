from pathlib import Path

file = Path("artifacts/report.txt")
assert file.is_file(), "report.txt 不存在"
assert "AGENT_BASE_DEMO_OK" in file.read_text(encoding="utf-8"), "报告缺少验收标记"
print("report verified")
