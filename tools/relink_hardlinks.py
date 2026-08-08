"""Upgrade peer symlinks under Save-to to same-volume hardlinks.

Explorer shows symlinks as 0-byte shortcuts; many viewers then fail to open
them. Hardlinks look like normal files (full size) while still deduping bytes.

Usage:
  python tools/relink_hardlinks.py
  python tools/relink_hardlinks.py "a:\\trt\\.Pics\\[Author] Title"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fs_links import ensure_symlink, resolve_real_file  # noqa: E402

DEFAULT_ROOT = Path(r"a:\trt\.Pics")


def relink_one(path: Path) -> str:
    """Return ``hard``, ``same``, ``skip``, or ``failed``."""
    if not path.is_symlink():
        return "skip"
    real = resolve_real_file(path)
    if real is None:
        return "failed"
    # ensure_symlink upgrades symlink → hardlink when on the same volume.
    status = ensure_symlink(path, real)
    if status in ("ok", "same"):
        try:
            if path.is_file() and not path.is_symlink() and path.samefile(real):
                return "hard" if status == "ok" else "same"
        except OSError:
            pass
        return "same" if status == "same" else "ok"
    return status


def iter_symlinks(root: Path):
    if root.is_file() or root.is_symlink():
        if root.is_symlink():
            yield root
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            p = base / name
            try:
                if p.is_symlink():
                    yield p
            except OSError:
                continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_ROOT),
        help=f"gallery folder or Save-to root (default: {DEFAULT_ROOT})",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N symlink upgrades (0 = no limit)",
    )
    args = ap.parse_args()
    root = Path(args.path)
    if not root.exists():
        print(f"not found: {root}")
        return 1

    counts = {"hard": 0, "same": 0, "skip": 0, "failed": 0, "ok": 0}
    seen = 0
    for link in iter_symlinks(root):
        seen += 1
        status = relink_one(link)
        counts[status] = counts.get(status, 0) + 1
        if status == "failed":
            print(f"failed: {link}")
        if args.limit and counts["hard"] >= args.limit:
            break

    print(
        f"scanned_symlinks={seen} "
        f"hard={counts.get('hard', 0)} "
        f"same={counts.get('same', 0)} "
        f"ok={counts.get('ok', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"skip={counts.get('skip', 0)}"
    )
    return 0 if counts.get("failed", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
