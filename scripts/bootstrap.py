"""Install pinned local runtimes; never installs anything globally."""
import argparse
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-dsh", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not shutil.which("uv"):
        raise SystemExit("请先安装 uv，再运行此脚本。")
    subprocess.run(["uv", "sync", "--frozen"], cwd=root, check=True)
    if not args.skip_dsh:
        if not shutil.which("node") or not shutil.which("npm"):
            raise SystemExit("dsh 需要 Node.js 24.9+ 和 npm。")
        runtime = root / ".runtime" / "dsh"
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(root / "runtime" / name, runtime / name)
        subprocess.run([shutil.which("npm"), "ci", "--prefix", str(runtime), "--no-audit", "--no-fund"], check=True)
        # This upstream postinstall only restores a packaged helper's executable
        # bit. New npm versions can block it by default; no network or build here.
        helper = runtime / "node_modules/@deepseek-ai/dsh-subprocess-local/scripts/ensure-spawn-helper.mjs"
        if helper.exists():
            subprocess.run(["node", str(helper)], check=True)
    executable = root / ".venv" / ("Scripts/agent-base.exe" if __import__("os").name == "nt" else "bin/agent-base")
    if not (root / "agent-base.toml").exists():
        subprocess.run([str(executable), "init"], cwd=root, check=True)
    subprocess.run([str(executable), "doctor"], cwd=root, check=True)
    print(f"启动：{executable} admin")


if __name__ == "__main__":
    main()
