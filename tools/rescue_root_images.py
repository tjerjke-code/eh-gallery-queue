"""Move images accidentally saved into Save-to *root* into gallery folders.

Cause: a brief bug reused queue ``out_dir`` even when it was the Save-to root,
so the downloader wrote into ``a:\\trt\\.Pics\\`` instead of
``a:\\trt\\.Pics\\{title}\\``.

Recovery:
  1. List loose image files in the Save-to root (not in subfolders).
  2. Resolve each file's ``gallery_key`` via SHA-1 fingerprints / aliases.
  3. Destination = existing gallery ``out_dir`` if it is a *subfolder*, else
     ``{root}/{sanitized title}/``.
  4. Move the file; refresh fingerprint ``sample_path``; fix DB ``out_dir``
     rows that still point at the root.

Usage:
  python tools/rescue_root_images.py          # dry-run
  python tools/rescue_root_images.py --apply  # move + DB updates
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import defaultdict
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from db import QueueStore, strip_order_prefix, index_pad_width

ROOT = Path(r"a:\trt\.Pics")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_name(name: str) -> str:
    name = _WIN_BAD.sub("_", name or "").strip(" .")
    return name[:180] or "gallery"


def sha1_file(path: Path) -> bytes:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.digest()


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).rstrip("\\/").casefold() == str(b).rstrip("\\/").casefold()


def _is_under_root_subdir(path: Path, root: Path) -> bool:
    """True if path is a directory strictly inside root (not root itself)."""
    if not path.is_dir():
        return False
    if _same_path(path, root):
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def list_root_images(root: Path) -> list[Path]:
    out = []
    for p in root.iterdir():
        if not p.is_file() or p.name.endswith(".part"):
            continue
        if p.suffix.lower() in IMAGE_EXT:
            out.append(p)
    return sorted(out, key=lambda x: x.name.lower())


def resolve_gallery_key(store: QueueStore, digest: bytes, filename: str) -> str | None:
    fp = store.lookup_sha1(digest)
    if fp and fp.get("gallery_key"):
        return str(fp["gallery_key"])
    for alias in store.list_name_aliases(digest):
        if alias.get("gallery_key"):
            return str(alias["gallery_key"])
    # Filename match against queue_images for items still pointing at root.
    bare = strip_order_prefix(filename, index_pad_width(9999))
    # Also try stripping any width.
    m = re.match(r"^(\d+)_(.+)$", filename, flags=re.DOTALL)
    if m:
        bare = m.group(2)
    with store._lock:
        row = store._conn().cursor().execute(
            """
            SELECT TOP 1 q.gallery_key
            FROM dbo.queue_images qi
            INNER JOIN dbo.queue_items q ON q.id = qi.queue_id
            WHERE qi.filename = ?
               OR qi.filename = ?
            ORDER BY q.id DESC
            """,
            filename[:260],
            bare[:260],
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def resolve_dest_folder(
    store: QueueStore, root: Path, gallery_key: str
) -> Path | None:
    gal = store.find_gallery_by_key(gallery_key)
    q = store.find_queue_by_key(gallery_key)
    meta = gal or q or {}
    out = (meta.get("out_dir") or "").strip()
    if out:
        p = Path(out)
        if _is_under_root_subdir(p, root):
            return p
    title = (meta.get("title") or "").strip()
    if not title:
        return None
    return root / sanitize_name(title)


def update_sample_paths(
    store: QueueStore, digest: bytes, *, old_path: Path, new_path: Path
) -> None:
    new_s = str(new_path)[:400]
    old_s = str(old_path)[:400]
    with store._lock:
        cur = store._conn().cursor()
        cur.execute(
            """
            UPDATE dbo.image_fingerprints
            SET sample_path = ?, updated_at = SYSUTCDATETIME()
            WHERE sha1 = ?
              AND (
                    sample_path IS NULL
                 OR LOWER(sample_path) = LOWER(?)
              )
            """,
            new_s,
            digest,
            old_s,
        )
        cur.execute(
            """
            UPDATE dbo.image_name_aliases
            SET sample_path = ?
            WHERE sha1 = ?
              AND (
                    sample_path IS NULL
                 OR LOWER(sample_path) = LOWER(?)
              )
            """,
            new_s,
            digest,
            old_s,
        )
        store._conn().commit()


def fix_gallery_out_dir(
    store: QueueStore, root: Path, gallery_key: str, dest: Path
) -> None:
    dest_s = str(dest)
    with store._lock:
        cur = store._conn().cursor()
        for table in ("dbo.galleries", "dbo.queue_items"):
            cur.execute(
                f"""
                UPDATE {table}
                SET out_dir = ?
                WHERE gallery_key = ?
                  AND (
                        out_dir IS NULL
                     OR LOWER(out_dir) = LOWER(?)
                     OR out_dir = ?
                  )
                """,
                dest_s[:512],
                gallery_key,
                str(root),
                str(root).rstrip("\\/"),
            )
        store._conn().commit()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help=f"Save-to root (default {ROOT})",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Move files and update DB (default is dry-run)",
    )
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"Root not found: {root}")
        return 1

    files = list_root_images(root)
    print(f"Loose images in {root}: {len(files)}")
    if not files:
        return 0

    store = QueueStore()
    by_gal: dict[str, list[tuple[Path, bytes]]] = defaultdict(list)
    unknown: list[Path] = []

    for path in files:
        digest = sha1_file(path)
        key = resolve_gallery_key(store, digest, path.name)
        if not key:
            unknown.append(path)
            continue
        by_gal[key].append((path, digest))

    moved = 0
    skipped = 0
    for key, items in sorted(by_gal.items(), key=lambda kv: kv[0]):
        dest = resolve_dest_folder(store, root, key)
        if dest is None:
            print(f"  [{key}] no title/out_dir — leave {len(items)} file(s)")
            skipped += len(items)
            continue
        print(f"  [{key}] → {dest.name}/  ({len(items)} file(s))")
        if args.apply:
            dest.mkdir(parents=True, exist_ok=True)
            fix_gallery_out_dir(store, root, key, dest)
        for path, digest in items:
            target = dest / path.name
            if target.exists():
                # Same name already in gallery folder — drop root copy if identical.
                try:
                    same = sha1_file(target) == digest
                except OSError:
                    same = False
                if same:
                    print(f"    delete dup {path.name}")
                    if args.apply:
                        path.unlink(missing_ok=True)
                        update_sample_paths(
                            store, digest, old_path=path, new_path=target
                        )
                    moved += 1
                    continue
                stem, dot, ext = path.name.rpartition(".")
                n = 1
                while True:
                    alt = dest / (f"{stem}_{n}.{ext}" if dot else f"{stem}_{n}")
                    if not alt.exists():
                        target = alt
                        break
                    n += 1
            print(f"    move {path.name} → {target.name}")
            if args.apply:
                path.rename(target)
                update_sample_paths(
                    store, digest, old_path=path, new_path=target
                )
            moved += 1

    if unknown:
        print(f"Unknown (no gallery_key): {len(unknown)}")
        for p in unknown[:20]:
            print(f"  ? {p.name}")
        if len(unknown) > 20:
            print(f"  … +{len(unknown) - 20} more")

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"{mode}: moved/resolved={moved} skipped={skipped} unknown={len(unknown)}")
    if not args.apply and (moved or unknown):
        print("Re-run with --apply to perform moves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
