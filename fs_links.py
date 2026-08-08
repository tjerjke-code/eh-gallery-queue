"""Link helpers for cross-gallery duplicate visibility (Windows-friendly).

Real bytes live at ``image_fingerprints.sample_path``. Other galleries get a
same-volume **hardlink** (falls back to symlink) so folders stay browsable and
image viewers see a normal non-zero file. DB aliases remain the source of truth
even when links are omitted.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_real_file(path: Path | str | None) -> Path | None:
    """Follow symlinks to the real file, or None if missing."""
    if not path:
        return None
    p = Path(path)
    try:
        if not p.exists():
            return None
        # resolve() follows symlinks; require a regular file at the end.
        real = p.resolve(strict=False)
        if real.is_file() and not real.is_symlink():
            return real
        if real.is_file():
            # Still a symlink loop or oddity — try readlink chain.
            seen: set[Path] = set()
            cur = p
            while cur.is_symlink():
                if cur in seen:
                    return None
                seen.add(cur)
                cur = (cur.parent / os.readlink(cur)).resolve(strict=False)
            return cur if cur.is_file() else None
        return None
    except OSError:
        return None


def same_path(a: Path | str | None, b: Path | str | None) -> bool:
    """True if both paths name the same bytes (symlinks + hardlinks)."""
    if not a or not b:
        return False
    try:
        pa, pb = Path(a), Path(b)
        if pa.exists() and pb.exists():
            return pa.samefile(pb)
        return pa.resolve() == pb.resolve()
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def same_entry(a: Path | str | None, b: Path | str | None) -> bool:
    """True if paths name the same directory entry (does not follow symlinks)."""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b))
        )
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _same_drive(a: Path, b: Path) -> bool:
    da = os.path.splitdrive(os.path.abspath(str(a)))[0].casefold()
    db = os.path.splitdrive(os.path.abspath(str(b)))[0].casefold()
    return bool(da) and da == db


def _link_to_target(link: Path, target: Path) -> bool:
    """Create hardlink when possible, else symlink. ``link`` must not exist."""
    if _same_drive(link, target):
        try:
            os.link(str(target), str(link))
            return True
        except OSError:
            pass
    try:
        try:
            rel = os.path.relpath(target, start=link.parent)
            os.symlink(rel, link, target_is_directory=False)
        except ValueError:
            os.symlink(str(target), link, target_is_directory=False)
        return True
    except OSError:
        try:
            os.symlink(str(target), link, target_is_directory=False)
            return True
        except OSError:
            return False


def ensure_symlink(link: Path, target: Path) -> str:
    """Ensure ``link`` refers to ``target`` bytes (hardlink preferred).

    Returns one of: ``ok``, ``exists_real``, ``same``, ``failed``.
    """
    link = Path(link)
    target = Path(target)
    if not target.is_file() or target.is_symlink():
        # Prefer a non-symlink endpoint when the target path is itself a link.
        resolved = resolve_real_file(target)
        if resolved is None or not resolved.is_file():
            return "failed"
        target = resolved
    link.parent.mkdir(parents=True, exist_ok=True)

    if link.exists() or link.is_symlink():
        if link.is_symlink():
            # Already points at target → upgrade symlink to hardlink when possible.
            if same_path(link, target):
                if not _same_drive(link, target):
                    return "same"
                try:
                    old_tgt = os.readlink(link)
                except OSError:
                    old_tgt = None
                try:
                    link.unlink()
                except OSError:
                    return "same"
                try:
                    os.link(str(target), str(link))
                    return "ok"
                except OSError:
                    # Restore symlink; do not leave a hole if hardlink is refused
                    # (e.g. NTFS per-file link limit).
                    if old_tgt is not None and not link.exists():
                        try:
                            os.symlink(old_tgt, link, target_is_directory=False)
                        except OSError:
                            _link_to_target(link, target)
                    return "same"
            try:
                link.unlink()
            except OSError:
                return "failed"
        elif link.is_file():
            # Real file / hardlink already here — do not replace a different file.
            if same_path(link, target):
                return "same"
            return "exists_real"
        else:
            return "failed"

    if _link_to_target(link, target):
        return "ok"
    return "failed"


def remove_path_if_link_or_dup(path: Path, *, real_keep: Path) -> bool:
    """Remove ``path`` if it is a symlink or a duplicate real file of ``real_keep``."""
    return strip_peer_presence(path, real_keep=real_keep) in ("link", "dup")


def remove_peer_any(path: Path, *, real_keep: Path) -> str:
    """Remove peer symlink or real file (any size). Never deletes ``real_keep``.

    Used after user-confirmed match-group Same (bytes may differ).
    Returns ``link``, ``file``, ``home``, ``missing``, or ``skip``.
    """
    path = Path(path)
    real_keep = Path(real_keep)
    if same_entry(path, real_keep):
        return "home"
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if not is_link and not path.exists():
        return "missing"
    try:
        if is_link:
            path.unlink()
            return "link"
        if path.is_file():
            path.unlink()
            return "file"
    except OSError:
        return "skip"
    return "skip"


def strip_peer_presence(path: Path, *, real_keep: Path) -> str:
    """Remove a peer symlink, hardlink, or same-size duplicate; keep ``real_keep``.

    Peer symlinks that *point at* ``real_keep`` must still be removed. Do not use
    resolve()-based identity for the home guard — that treats those links as home.

    Returns ``link``, ``dup``, ``home``, ``missing``, or ``skip``.
    """
    path = Path(path)
    real_keep = Path(real_keep)
    if same_entry(path, real_keep):
        return "home"
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = False
    if not is_link and not path.exists():
        return "missing"
    try:
        if is_link:
            path.unlink()
            return "link"
        if path.is_file() and real_keep.is_file():
            try:
                if path.samefile(real_keep):
                    path.unlink()
                    return "link"
            except OSError:
                pass
            try:
                if path.stat().st_size != real_keep.stat().st_size:
                    return "skip"
            except OSError:
                return "skip"
            path.unlink()
            return "dup"
    except OSError:
        return "skip"
    return "skip"


def move_real_file(src: Path, dest: Path) -> Path:
    """Move a real file to ``dest`` (replacing a symlink at dest if present)."""
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    real = resolve_real_file(src) or src
    if real.is_symlink():
        raise OSError(f"source is still a symlink: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        # Symlink at dest that points at real must be replaced with the bytes.
        if dest.is_symlink():
            dest.unlink()
        elif same_entry(dest, real) or same_path(dest, real):
            return dest
        elif dest.is_file():
            raise FileExistsError(f"destination already has a real file: {dest}")
    real.replace(dest)
    return dest
