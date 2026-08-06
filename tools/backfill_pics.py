"""Backfill fingerprints + order-prefix renames under a:\\trt\\.Pics.

- Prefer EH queue_images order (page number) when the gallery is in DB.
- Else natural-sort files in the folder.
- Pad width from gallery total (DB image_total or file count): 01_/001_/….
- Rename bare thumb names → ``{idx}_{name}``; register SHA-1; refresh queue_images.
- Does not download anything.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from db import (
    QueueStore,
    apply_order_prefix,
    index_pad_width,
    strip_order_prefix,
)

ROOT = Path(r"a:\trt\.Pics")
SKIP_DIRS = {".Sort", ".sort"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _sha1_file(path: Path, chunk: int = 1024 * 1024) -> bytes:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.digest()


def _nat_key(name: str):
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r"(\d+)", name)
    ]


def _page_no(url: str) -> int:
    m = re.search(r"/(\d+)-(\d+)/?$", (url or "").split("?")[0])
    return int(m.group(2)) if m else 10**9


def _list_images(folder: Path) -> list[Path]:
    out = []
    for p in folder.iterdir():
        if not p.is_file() or p.name.endswith(".part"):
            continue
        if p.suffix.lower() in IMAGE_EXT:
            out.append(p)
    return out


def _queue_plan(store: QueueStore, folder: Path) -> tuple[list[tuple[str | None, str]], int, str | None]:
    """Return ([(page_url|None, bare_filename), …], width_total, gallery_key)."""
    cur = store._conn().cursor()
    # Match folder by out_dir or title
    row = cur.execute(
        """
        SELECT TOP 1 id, gallery_key, image_total, url
        FROM dbo.queue_items
        WHERE out_dir = ? OR title = ?
        ORDER BY id DESC
        """,
        str(folder),
        folder.name,
    ).fetchone()
    if not row:
        row = cur.execute(
            """
            SELECT TOP 1 id, gallery_key, image_total, url
            FROM dbo.galleries
            WHERE out_dir = ? OR title = ?
            ORDER BY id DESC
            """,
            str(folder),
            folder.name,
        ).fetchone()
        if row:
            # completed gallery — no queue_images; fall back to disk order
            gkey = row[1]
            total = int(row[2] or 0)
            files = sorted(_list_images(folder), key=lambda p: _nat_key(p.name))
            wtotal = max(len(files), total, 1)
            plan = []
            w = index_pad_width(wtotal)
            for f in files:
                bare = strip_order_prefix(f.name, w)
                plan.append((None, bare))
            return plan, wtotal, gkey
        files = sorted(_list_images(folder), key=lambda p: _nat_key(p.name))
        wtotal = max(len(files), 1)
        w = index_pad_width(wtotal)
        return [(None, strip_order_prefix(f.name, w)) for f in files], wtotal, None

    qid, gkey, image_total, gurl = int(row[0]), row[1], row[2], row[3]
    imgs = cur.execute(
        """
        SELECT page_url, filename FROM dbo.queue_images
        WHERE queue_id = ?
        """,
        qid,
    ).fetchall()
    if not imgs:
        files = sorted(_list_images(folder), key=lambda p: _nat_key(p.name))
        wtotal = max(len(files), int(image_total or 0), 1)
        w = index_pad_width(wtotal)
        return [(None, strip_order_prefix(f.name, w)) for f in files], wtotal, gkey

    ordered = sorted(imgs, key=lambda r: _page_no(r[0]))
    # Bare names currently in DB (may already be ordered — strip guessing with width from total)
    wtotal = max(len(ordered), int(image_total or 0), 1)
    w = index_pad_width(wtotal)
    plan = []
    for page_url, filename in ordered:
        bare = strip_order_prefix(filename, w)
        plan.append((page_url, bare))
    return plan, wtotal, gkey


def process_folder(store: QueueStore, folder: Path) -> dict:
    plan, wtotal, gkey = _queue_plan(store, folder)
    disk = {p.name: p for p in _list_images(folder)}
    w = index_pad_width(wtotal)
    # Map bare → path (also allow already-ordered names)
    by_bare: dict[str, Path] = {}
    for name, path in disk.items():
        bare = strip_order_prefix(name, w)
        by_bare.setdefault(bare, path)
        by_bare.setdefault(name, path)

    renamed = 0
    fingerprinted = 0
    missing = 0
    new_links: list[tuple[str, str]] = []
    used_paths: set[Path] = set()

    for i, (page_url, bare) in enumerate(plan, start=1):
        ordered = apply_order_prefix(i, bare, wtotal)
        src = by_bare.get(bare) or by_bare.get(ordered)
        if src is None or not src.is_file():
            missing += 1
            if page_url:
                new_links.append((page_url, ordered))
            continue
        dest = folder / ordered
        if src.resolve() != dest.resolve():
            if dest.exists():
                # Already have the ordered name — do not leave src for leftovers
                # (that previously re-indexed the same bytes under a new name).
                used_paths.add(src.resolve())
                try:
                    if _sha1_file(src) == _sha1_file(dest) and src.resolve() != dest.resolve():
                        src.unlink()
                except OSError:
                    pass
                src = dest
            else:
                src.rename(dest)
                renamed += 1
                src = dest
        used_paths.add(src.resolve())
        digest = _sha1_file(src)
        store.register_sha1(
            digest,
            src.stat().st_size,
            sample_path=str(src),
            gallery_key=gkey,
            name=ordered,
            bare_name=bare,
        )
        fingerprinted += 1
        if page_url:
            new_links.append((page_url, ordered))

    # Leftover disk files not in plan — fingerprint + order after plan
    leftovers = [
        p for p in _list_images(folder)
        if p.resolve() not in used_paths
    ]
    leftovers.sort(key=lambda p: _nat_key(p.name))
    idx = len(plan) + 1
    # Recompute width if leftovers push total up
    final_total = max(wtotal, len(plan), idx - 1 + len(leftovers))
    for p in leftovers:
        bare_left = strip_order_prefix(p.name, w)
        ordered = apply_order_prefix(idx, bare_left, final_total)
        dest = folder / ordered
        if p.resolve() != dest.resolve():
            if dest.exists():
                used_paths.add(p.resolve())
                try:
                    if _sha1_file(p) == _sha1_file(dest):
                        p.unlink()
                        p = dest
                    else:
                        # different bytes; keep as next index under a unique name
                        ordered = apply_order_prefix(
                            idx, bare_left + "_extra", final_total
                        )
                        dest = folder / ordered
                        if not dest.exists():
                            p.rename(dest)
                            renamed += 1
                            p = dest
                except OSError:
                    pass
            else:
                p.rename(dest)
                renamed += 1
                p = dest
        if not p.exists():
            idx += 1
            continue
        used_paths.add(p.resolve())
        digest = _sha1_file(p)
        store.register_sha1(
            digest,
            p.stat().st_size,
            sample_path=str(p),
            gallery_key=gkey,
            name=ordered,
            bare_name=bare_left,
        )
        fingerprinted += 1
        idx += 1

    # Refresh queue_images filenames to ordered form when we had URLs
    if gkey and new_links:
        # Rebuild full ordered list for all plan slots (including missing)
        full = []
        for i, (page_url, bare) in enumerate(plan, start=1):
            if not page_url:
                continue
            full.append((page_url, apply_order_prefix(i, bare, wtotal)))
        # Use gallery url from queue item
        cur = store._conn().cursor()
        row = cur.execute(
            "SELECT url FROM dbo.queue_items WHERE gallery_key = ?", gkey
        ).fetchone()
        if row and row[0] and full:
            store.replace_images(row[0], full)

    return {
        "folder": folder.name,
        "planned": len(plan),
        "renamed": renamed,
        "fingerprinted": fingerprinted,
        "missing_slots": missing,
        "leftovers": len(leftovers),
        "pad": index_pad_width(wtotal),
        "gallery_key": gkey,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not ROOT.is_dir():
        print(f"Missing {ROOT}")
        return 1
    store = QueueStore()
    reports = []
    for folder in sorted(ROOT.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name in SKIP_DIRS:
            continue
        if not _list_images(folder):
            print(f"skip empty {folder.name!r}")
            continue
        print(f"processing {folder.name!r} …")
        rep = process_folder(store, folder)
        reports.append(rep)
        print(
            f"  pad={rep['pad']} planned={rep['planned']} renamed={rep['renamed']} "
            f"fp={rep['fingerprinted']} missing={rep['missing_slots']} "
            f"leftovers={rep['leftovers']}"
        )
    print("--- done ---")
    print(f"fingerprints now: {store._conn().cursor().execute('select count(*) from dbo.image_fingerprints').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
