from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from career_agent_common import BASE, JOBS_DIR, LOGS_DIR, PDF_PREVIEWS_DIR

RULES = [
    (LOGS_DIR, "*.log", 30),
    (LOGS_DIR, "*_debug.json", 14),
    (PDF_PREVIEWS_DIR, "*", 14),
    (JOBS_DIR / "snapshots", "*.json", 120),
]


def within_base(path: Path) -> bool:
    try:
        path.resolve().relative_to(BASE.resolve())
        return True
    except ValueError:
        return False


def expired_files(now: datetime | None = None) -> list[tuple[Path, int]]:
    now = now or datetime.now()
    output: list[tuple[Path, int]] = []
    for root, pattern, days in RULES:
        if not root.exists() or not within_base(root):
            continue
        cutoff = now - timedelta(days=days)
        for path in root.rglob(pattern):
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff and within_base(path):
                output.append((path, days))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply bounded retention rules to logs, previews, and job snapshots.")
    parser.add_argument("--apply", action="store_true", help="Delete expired files; default is a dry run")
    args = parser.parse_args()
    files = expired_files()
    for path, days in files:
        print(f"{'DELETE' if args.apply else 'WOULD_DELETE'} after {days} days: {path}")
        if args.apply:
            path.unlink(missing_ok=True)
    print(f"Expired files: {len(files)}")


if __name__ == "__main__":
    main()
