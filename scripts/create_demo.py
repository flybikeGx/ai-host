"""Create a dedicated Git demo without modifying the example sources."""
import argparse
import shutil
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = (args.destination or root / ".agent-base" / "demo-repo").resolve()
    if target.exists():
        raise SystemExit(f"保留已有目录：{target}")
    shutil.copytree(root / "examples" / "hello-agent", target)
    subprocess.run(["git", "init", "--quiet", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "-c", "user.name=Agent Base Demo", "-c", "user.email=demo@example.invalid",
                    "-c", "core.hooksPath=" + str(root / ".agent-base" / "empty-hooks"), "commit", "--quiet", "-m", "Local demo"], check=True)
    print(target)


if __name__ == "__main__":
    main()
