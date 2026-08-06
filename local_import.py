"""Local gallery folder scan + fingerprint registration for Import.

Shared by the Import UI and ``tools/backfill_pics.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable
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
# Top-level archives under Save-to are extracted to a folder named after the stem.
ZIP_ARCHIVE_EXT = {".zip", ".cbz"}
SEVEN_Z_ARCHIVE_EXT = {".rar", ".cbr", ".7z"}
ARCHIVE_EXT = ZIP_ARCHIVE_EXT | SEVEN_Z_ARCHIVE_EXT

log = logging.getLogger("EHGalleryQueue.local_import")


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


def is_archive(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in ARCHIVE_EXT


def _find_7z() -> Path | None:
    for name in ("7z", "7z.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _safe_zip_members(zf: zipfile.ZipFile, dest: Path) -> list[zipfile.ZipInfo]:
    """Reject Zip Slip paths; return members safe to extract under ``dest``."""
    dest_res = dest.resolve()
    safe: list[zipfile.ZipInfo] = []
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        try:
            target.relative_to(dest_res)
        except ValueError as e:
            raise ValueError(f"unsafe path in archive: {info.filename!r}") from e
        safe.append(info)
    return safe


def _unwrap_single_child_dir(dest: Path) -> None:
    """If extract produced one subfolder and no top-level images, lift contents up."""
    try:
        children = [p for p in dest.iterdir() if p.name not in SKIP_DIRS]
    except OSError:
        return
    if list_images(dest):
        return
    subdirs = [p for p in children if p.is_dir()]
    files = [p for p in children if p.is_file()]
    if len(subdirs) != 1 or files:
        return
    inner = subdirs[0]
    try:
        for item in list(inner.iterdir()):
            target = dest / item.name
            if target.exists():
                continue
            item.rename(target)
        # Remove empty leftovers (ignore non-empty / locked).
        for leftover in list(inner.iterdir()):
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
        inner.rmdir()
    except OSError:
        pass


def _extract_zip_archive(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        members = _safe_zip_members(zf, dest)
        zf.extractall(dest, members=members)
    _unwrap_single_child_dir(dest)


def _extract_with_7z(archive: Path, dest: Path, seven_z: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # eXtract with full paths into dest; -y = assume Yes on prompts.
    proc = subprocess.run(
        [str(seven_z), "x", str(archive), f"-o{dest}", "-y", "-bso0", "-bsp0"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"7z failed: {err[:200]}")
    _unwrap_single_child_dir(dest)


def extract_toplevel_archives(
    root: Path,
    *,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Extract top-level archives under ``root`` into folders named after each stem.

    Skips an archive when a same-named folder already contains images.
    Returns counts: extracted / skipped / failed / errors.
    """
    root = Path(root)
    stats: dict = {
        "extracted": 0,
        "removed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    if not root.is_dir():
        return stats

    archives = sorted(
        (p for p in root.iterdir() if is_archive(p)),
        key=lambda p: p.name.lower(),
    )
    if not archives:
        return stats

    seven_z = _find_7z()
    total = len(archives)
    for i, archive in enumerate(archives, start=1):
        if should_stop and should_stop():
            break
        name = archive.stem
        dest = root / name
        if progress:
            progress(f"Extracting archive {i}/{total}: {archive.name[:60]}")

        if dest.is_dir() and list_images(dest):
            stats["skipped"] += 1
            continue
        if dest.exists() and not dest.is_dir():
            msg = f"skip {archive.name}: path exists and is not a folder"
            stats["failed"] += 1
            stats["errors"].append(msg)
            log.warning(msg)
            continue

        ext = archive.suffix.lower()
        try:
            if ext in ZIP_ARCHIVE_EXT:
                _extract_zip_archive(archive, dest)
            elif ext in SEVEN_Z_ARCHIVE_EXT:
                if seven_z is None:
                    raise RuntimeError(
                        "need 7-Zip (7z.exe) on PATH or in Program Files for "
                        f"{ext} archives"
                    )
                _extract_with_7z(archive, dest, seven_z)
            else:
                raise RuntimeError(f"unsupported archive type: {ext}")

            if not list_images(dest):
                raise RuntimeError("no images after extract")
            stats["extracted"] += 1
            log.info("Extracted archive %s → %s/", archive.name, name)
            try:
                archive.unlink()
                stats["removed"] += 1
            except OSError as e:
                msg = f"could not delete {archive.name}: {e}"
                stats["errors"].append(msg)
                log.warning(msg)
        except Exception as e:
            stats["failed"] += 1
            err = f"{archive.name}: {e}"
            stats["errors"].append(err)
            log.warning("Archive extract failed: %s", err)

    return stats


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
