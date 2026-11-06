"""Remove stale per-request dirs in tmp/. Dry-run by default.

    .venv/bin/python cleanup.py              # preview
    .venv/bin/python cleanup.py --delete     # actually delete
    .venv/bin/python cleanup.py --max-age 5  # minutes
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from config import load_settings


def stale_dirs(tmp_dir: Path, max_age_min: float) -> list[Path]:
    cutoff = time.time() - max_age_min * 60
    if not tmp_dir.is_dir():
        return []
    return [p for p in tmp_dir.iterdir() if p.is_dir() and p.stat().st_mtime < cutoff]


def delete(dirs: list[Path]) -> None:
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-age", type=float, default=60, help="minutes (default 60)")
    ap.add_argument("--delete", action="store_true", help="really delete (default: dry-run)")
    args = ap.parse_args()

    s = load_settings(require_token=False)
    dirs = stale_dirs(s.tmp_dir, args.max_age)
    if not dirs:
        print("nothing to clean")
        return
    for d in dirs:
        print(("DELETE " if args.delete else "would delete ") + str(d))
    if args.delete:
        delete(dirs)
    else:
        print(f"\n{len(dirs)} dir(s). dry-run only, pass --delete to remove.")


if __name__ == "__main__":
    main()
