"""Remove accidental duplicate files created by an earlier backfill collision bug."""

from __future__ import annotations

import hashlib
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

ROOT = Path(r"a:\trt\.Pics")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def sha1_file(path: Path) -> bytes:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.digest()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    removed = 0
    for folder in ROOT.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        by_hash: dict[bytes, list[Path]] = defaultdict(list)
        for p in folder.iterdir():
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
                continue
            by_hash[sha1_file(p)].append(p)
        for digest, paths in by_hash.items():
            if len(paths) < 2:
                continue
            # Prefer names that look like zero-padded order prefixes.
            def score(p: Path) -> tuple:
                n = p.name
                pad = 0
                if len(n) > 3 and n[0].isdigit() and "_" in n:
                    left = n.split("_", 1)[0]
                    if left.isdigit() and len(left) >= 2:
                        pad = len(left)
                return (-pad, len(n), n)

            paths_sorted = sorted(paths, key=score)
            keep = paths_sorted[0]
            for dup in paths_sorted[1:]:
                print(f"remove dup {dup} (keep {keep.name})")
                dup.unlink()
                removed += 1
    print(f"removed {removed} duplicate files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
