from pathlib import Path

marker = Path("installation-count.txt")
previous = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(previous + 1))
print("repo initialized")
