"""Local gallery folder scan + fingerprint registration for Import.

Shared by the Import UI and ``tools/backfill_pics.py``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from db import (
    QueueStore,
    apply_order_prefix,
    gallery_key_from_url,
    index_pad_width,
    strip_order_prefix,
)

SKIP_DIRS = {".Sort", ".sort"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def sha1_file(path: Path, chunk: int = 1024 * 1024) -> bytes:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.digest()


def nat_key(name: str):
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r"(\d+)", name)
    ]


def list_images(folder: Path) -> list[Path]:
    out = []
    for p in folder.iterdir():
        if not p.is_file() or p.name.endswith(".part"):
            continue
        if p.suffix.lower() in IMAGE_EXT:
            out.append(p)
    return out


def scan_gallery_folders(root: Path) -> list[Path]:
    """Immediate child dirs under pics root that contain images."""
    root = Path(root)
    if not root.is_dir():
        return []
    folders = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name in SKIP_DIRS:
            continue
        if list_images(folder):
            folders.append(folder)
    return folders


def fingerprint_folder(
    store: QueueStore,
    folder: Path,
    *,
    gallery_key: str | None = None,
    image_total: int | None = None,
) -> dict:
    """Order-prefix rename + SHA-1 register for every image in ``folder``.

    Natural-sort order when no page plan is available (Import path).
    """
    folder = Path(folder)
    files = sorted(list_images(folder), key=lambda p: nat_key(p.name))
    wtotal = max(len(files), int(image_total or 0), 1)
    w = index_pad_width(wtotal)

    renamed = 0
    fingerprinted = 0
    used: set[Path] = set()

    by_bare: dict[str, Path] = {}
    for path in files:
        bare = strip_order_prefix(path.name, w)
        by_bare.setdefault(bare, path)
        by_bare.setdefault(path.name, path)

    # Prefer existing ordered names in sequence when present.
    plan_bares: list[str] = []
    for path in files:
        bare = strip_order_prefix(path.name, w)
        if bare not in plan_bares:
            plan_bares.append(bare)

    for i, bare in enumerate(plan_bares, start=1):
        ordered = apply_order_prefix(i, bare, wtotal)
        src = by_bare.get(bare) or by_bare.get(ordered)
        if src is None or not src.is_file():
            continue
        dest = folder / ordered
        if src.resolve() != dest.resolve():
            if dest.exists():
                used.add(src.resolve())
                try:
                    if sha1_file(src) == sha1_file(dest) and src.resolve() != dest.resolve():
                        src.unlink()
                except OSError:
                    pass
                src = dest
            else:
                src.rename(dest)
                renamed += 1
                src = dest
        used.add(src.resolve())
        digest = sha1_file(src)
        store.register_sha1(
            digest,
            src.stat().st_size,
            sample_path=str(src),
            gallery_key=gallery_key,
            name=ordered,
            bare_name=bare,
        )
        try:
            store.enqueue_sha_check(digest)
        except Exception:
            pass
        fingerprinted += 1

    return {
        "folder": folder.name,
        "renamed": renamed,
        "fingerprinted": fingerprinted,
        "image_count": len(files),
        "pad": w,
        "gallery_key": gallery_key,
    }


def import_local_gallery(
    store: QueueStore,
    folder: Path,
    url: str,
    *,
    title: str | None = None,
    fingerprint: bool = True,
) -> dict:
    """Register folder as completed in ``galleries`` (+ optional fingerprints)."""
    folder = Path(folder)
    files = list_images(folder)
    n = len(files)
    store.complete_gallery(
        url,
        title=title or folder.name,
        out_dir=str(folder),
        image_total=n,
        saved=0,
        skipped=n,
        failed=0,
    )
    key = gallery_key_from_url(url)
    fp: dict = {}
    if fingerprint:
        fp = fingerprint_folder(store, folder, gallery_key=key, image_total=n)
    return {
        "gallery_key": key,
        "url": url,
        "title": title or folder.name,
        "out_dir": str(folder),
        "image_count": n,
        **fp,
    }
