"""Standalone form: python scripts/check_prompt_paths.py repo-a repo-b."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_base.path_lint import check_prompts

issues = []
for argument in sys.argv[1:] or ["."]:
    root = Path(argument)
    if not root.is_dir():
        raise SystemExit(f"目录不存在：{root}")
    issues.extend({"repo": argument, **item} for item in check_prompts(root))
print(json.dumps({"ok": not issues, "issues": issues}, ensure_ascii=False, indent=2))
raise SystemExit(bool(issues))
