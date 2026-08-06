"""Pixiv ajax client + fill missing pages into an EH gallery folder.

Illust multi-page originals use bare names ``{illustId}_p{N}.{ext}`` (same as
EH thumb titles). Missing pages are downloaded into the folder that already
owns the set, then synth-prefixed by Pixiv page order and fingerprinted.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import requests

from db import QueueStore, apply_order_prefix, index_pad_width, strip_order_prefix
from downloader import sanitize_name
from local_import import list_images, sha1_file
from name_pattern import strip_synth_prefix

log = logging.getLogger("EHGalleryQueue.pixiv")

PIXIV_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
SETTING_PHPSESSID = "pixiv_phpsessid"
SETTING_USER_ID = "pixiv_user_id"
SETTING_USER_NAME = "pixiv_user_name"
SETTING_AUTH_OK_AT = "pixiv_auth_ok_at"
ILLUST_ID_RE = re.compile(r"(?:artworks/|/illusts?/)?(\d{6,})", re.I)
# After stripping synth prefix: 126324413_p0.jpg
PIXIV_BARE_RE = re.compile(
    r"^(?P<illust>\d{6,})_p(?P<page>\d+)\.(?P<ext>[a-z0-9]+)$",
    re.I,
)
# Browser PHPSESSID is usually ``{userId}_{token}``.
_PHPSESSID_USER_RE = re.compile(r"^(\d+)_")


class PixivError(RuntimeError):
    pass


def normalize_phpsessid(raw: str | None) -> str:
    """Extract PHPSESSID value from a raw paste or full Cookie header."""
    cookie = (raw or "").strip()
    if not cookie:
        return ""
    if "PHPSESSID=" in cookie.upper() or ";" in cookie:
        for part in cookie.split(";"):
            part = part.strip()
            if part.upper().startswith("PHPSESSID="):
                return part.split("=", 1)[1].strip()
        # Fall through if label missing but paste was cookie-like.
    return cookie


def user_id_from_phpsessid(phpsessid: str | None) -> str | None:
    """Pixiv web sessions encode the account id before the first ``_``."""
    m = _PHPSESSID_USER_RE.match(normalize_phpsessid(phpsessid) or "")
    return m.group(1) if m else None


def load_auth(store: QueueStore | None) -> dict:
    """Load persisted Pixiv login from ``app_settings``."""
    empty = {
        "phpsessid": "",
        "user_id": "",
        "user_name": "",
        "auth_ok_at": "",
    }
    if store is None:
        return empty
    try:
        return {
            "phpsessid": normalize_phpsessid(store.get_setting(SETTING_PHPSESSID) or ""),
            "user_id": (store.get_setting(SETTING_USER_ID) or "").strip(),
            "user_name": (store.get_setting(SETTING_USER_NAME) or "").strip(),
            "auth_ok_at": (store.get_setting(SETTING_AUTH_OK_AT) or "").strip(),
        }
    except Exception:
        log.exception("load_auth failed")
        return empty


def save_auth(
    store: QueueStore | None,
    phpsessid: str | None,
    *,
    user_id: str | None = None,
    user_name: str | None = None,
    verified: bool = False,
) -> dict:
    """Persist PHPSESSID (+ optional profile) for reuse on later requests."""
    if store is None:
        raise PixivError("database not connected")
    cookie = normalize_phpsessid(phpsessid)
    uid = (user_id or "").strip() or (user_id_from_phpsessid(cookie) or "")
    uname = (user_name or "").strip()
    store.set_setting(SETTING_PHPSESSID, cookie)
    if uid:
        store.set_setting(SETTING_USER_ID, uid)
    if uname:
        store.set_setting(SETTING_USER_NAME, uname)
    if verified:
        from datetime import datetime, timezone

        store.set_setting(
            SETTING_AUTH_OK_AT,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    elif not cookie:
        store.set_setting(SETTING_USER_ID, "")
        store.set_setting(SETTING_USER_NAME, "")
        store.set_setting(SETTING_AUTH_OK_AT, "")
    return load_auth(store)


def auth_status_text(auth: dict | None) -> str:
    """Short UI line for saved login state."""
    auth = auth or {}
    cookie = auth.get("phpsessid") or ""
    if not cookie:
        return "Not logged in — paste PHPSESSID and Save"
    name = (auth.get("user_name") or "").strip()
    uid = (auth.get("user_id") or user_id_from_phpsessid(cookie) or "").strip()
    when = (auth.get("auth_ok_at") or "").strip()
    who = name or (f"user {uid}" if uid else "cookie saved")
    if when:
        return f"Saved login: {who} · last ok {when}"
    return f"Saved login: {who} · not verified yet"


def make_session(phpsessid: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": PIXIV_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.pixiv.net/",
        }
    )
    cookie = normalize_phpsessid(phpsessid)
    if cookie:
        s.cookies.set("PHPSESSID", cookie, domain=".pixiv.net")
    return s


def session_from_store(store: QueueStore | None) -> requests.Session:
    """Build a requests session using the DB-saved PHPSESSID."""
    return make_session(load_auth(store).get("phpsessid") or "")


def session_phpsessid(session: requests.Session) -> str:
    """Current PHPSESSID from the cookie jar (may rotate after responses)."""
    try:
        for c in session.cookies:
            if c.name == "PHPSESSID" and c.value:
                return c.value.strip()
    except Exception:
        pass
    return ""


def persist_session_cookies(store: QueueStore | None, session: requests.Session) -> bool:
    """If Pixiv rotated PHPSESSID, write the new value back to DB."""
    if store is None:
        return False
    current = session_phpsessid(session)
    if not current:
        return False
    saved = normalize_phpsessid(load_auth(store).get("phpsessid") or "")
    if current == saved:
        return False
    auth = load_auth(store)
    save_auth(
        store,
        current,
        user_id=auth.get("user_id") or user_id_from_phpsessid(current),
        user_name=auth.get("user_name") or None,
        verified=bool(auth.get("auth_ok_at")),
    )
    log.info("pixiv PHPSESSID rotated — saved to DB")
    return True


def verify_login(
    store: QueueStore | None = None,
    *,
    phpsessid: str | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Validate session against Pixiv and persist profile on success.

    Uses ``/ajax/user/{id}`` where ``id`` comes from the PHPSESSID prefix.
    """
    cookie = normalize_phpsessid(phpsessid)
    sess = session
    if sess is None:
        if cookie:
            sess = make_session(cookie)
        else:
            sess = session_from_store(store)
            cookie = session_phpsessid(sess)
    else:
        cookie = cookie or session_phpsessid(sess)
    if not cookie:
        raise PixivError("no PHPSESSID — paste from browser and Save")
    uid = user_id_from_phpsessid(cookie)
    if not uid:
        raise PixivError(
            "PHPSESSID should look like '{userId}_{token}' "
            "(copy the full value from DevTools → Cookies)"
        )
    sess.headers["Referer"] = f"https://www.pixiv.net/users/{uid}"
    data = _ajax_get(sess, f"https://www.pixiv.net/ajax/user/{uid}")
    persist_session_cookies(store, sess)
    body = data.get("body") or {}
    name = (body.get("name") or "").strip()
    auth = save_auth(
        store,
        session_phpsessid(sess) or cookie,
        user_id=uid,
        user_name=name or None,
        verified=True,
    )
    return {
        "ok": True,
        "user_id": uid,
        "user_name": name,
        "auth": auth,
        "session": sess,
    }


def _ajax_get(session: requests.Session, url: str, *, timeout: float = 30) -> dict:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise PixivError(str(data.get("message") or "pixiv ajax error"))
    return data


def parse_illust_id(text: str) -> str | None:
    """Accept raw id or pixiv.net/artworks/{id} URL."""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 6:
        return s
    m = ILLUST_ID_RE.search(s)
    return m.group(1) if m else None


def pixiv_bare_name(illust_id: str, page: int, ext: str) -> str:
    ext = (ext or "jpg").lstrip(".").lower()
    if ext == "jpeg":
        ext = "jpg"
    return f"{illust_id}_p{int(page)}.{ext}"


def parse_pixiv_bare(filename: str) -> dict | None:
    """Parse ``[NNN_]{illust}_p{page}.{ext}`` → illust/page/ext."""
    bare = strip_synth_prefix(Path(filename or "").name)
    m = PIXIV_BARE_RE.match(bare)
    if not m:
        return None
    return {
        "illust": m.group("illust"),
        "page": int(m.group("page")),
        "ext": m.group("ext").lower(),
        "bare": bare,
    }


def pixiv_gallery_key(illust_id: str) -> str:
    """Stable ``galleries.gallery_key`` for a Pixiv-only set (not an EH id)."""
    return f"pixiv_{illust_id}"[:64]


def pixiv_gallery_url(illust_id: str) -> str:
    return f"https://www.pixiv.net/artworks/{illust_id}"


def illust_display_title(meta: dict | None) -> str:
    meta = meta or {}
    return (
        (meta.get("title") or meta.get("illustTitle") or meta.get("alt") or "")
        .strip()
    )


def illust_author_name(meta: dict | None) -> str:
    meta = meta or {}
    return (meta.get("userName") or meta.get("userAccount") or "").strip()


def gallery_folder_name(meta: dict | None, *, illust_id: str | None = None) -> str:
    """``[Author] Title`` — same shape as EH-imported Pixiv sets under Save-to."""
    author = illust_author_name(meta) or "pixiv"
    title = illust_display_title(meta) or (f"illust {illust_id}" if illust_id else "illust")
    raw = f"[{author}] {title}"
    return sanitize_name(raw)


def ensure_pixiv_gallery(
    store: QueueStore | None,
    pics_root: Path | str,
    illust_id: str,
    meta: dict | None,
    *,
    image_total: int | None = None,
) -> dict:
    """Create ``[Author] Title`` under Save-to and register a Pixiv gallery row.

    Reuses an existing ``pixiv_{id}`` out_dir or an already-matching folder name.
    """
    illust_id = str(illust_id)
    pics_root = Path(pics_root)
    pics_root.mkdir(parents=True, exist_ok=True)
    gkey = pixiv_gallery_key(illust_id)
    url = pixiv_gallery_url(illust_id)
    title = gallery_folder_name(meta, illust_id=illust_id)
    folder: Path | None = None

    if store is not None:
        gal = store.find_gallery_by_key(gkey) or {}
        od = (gal.get("out_dir") or "").strip()
        if od:
            cand = Path(od)
            if cand.is_dir() or not cand.exists():
                folder = cand

    if folder is None:
        folder = pics_root / title
        # Avoid clobbering an unrelated folder with the same display name.
        if folder.exists() and not folder.is_dir():
            folder = pics_root / sanitize_name(f"{title} ({illust_id})")
        elif folder.is_dir():
            local = scan_local_pages(folder, illust_id)
            other = [
                p
                for p in list_images(folder)
                if not parse_pixiv_bare(p.name)
                or parse_pixiv_bare(p.name)["illust"] != illust_id
            ]
            if other and not local:
                folder = pics_root / sanitize_name(f"{title} ({illust_id})")

    folder.mkdir(parents=True, exist_ok=True)
    total = image_total
    if total is None and meta:
        try:
            total = int(meta.get("pageCount") or 0) or None
        except (TypeError, ValueError):
            total = None

    if store is not None:
        store.upsert_gallery(
            gkey,
            url=url,
            title=title,
            out_dir=str(folder),
            image_total=total,
            token=None,
            saved=0,
            skipped=0,
            failed=0,
        )

    return {
        "gallery_key": gkey,
        "url": url,
        "title": title,
        "folder": folder,
        "out_dir": str(folder),
        "image_total": total,
        "created": True,
    }


def fetch_illust_meta(session: requests.Session, illust_id: str) -> dict:
    """``/ajax/illust/{id}`` body (title, pageCount, userName, …)."""
    url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
    session.headers["Referer"] = f"https://www.pixiv.net/artworks/{illust_id}"
    data = _ajax_get(session, url)
    body = data.get("body") or {}
    if not body:
        raise PixivError(f"empty illust meta for {illust_id}")
    return body


def fetch_illust_pages(session: requests.Session, illust_id: str) -> list[dict]:
    """List of ``{page, original_url, ext}`` for each page (0-based)."""
    url = f"https://www.pixiv.net/ajax/illust/{illust_id}/pages"
    session.headers["Referer"] = f"https://www.pixiv.net/artworks/{illust_id}"
    data = _ajax_get(session, url)
    body = data.get("body")
    if not isinstance(body, list) or not body:
        raise PixivError(f"no pages for {illust_id}")
    pages: list[dict] = []
    for i, item in enumerate(body):
        urls = (item or {}).get("urls") or {}
        original = (urls.get("original") or "").strip()
        if not original:
            raise PixivError(f"page {i} missing original url")
        path = urlparse(original).path
        ext = Path(path).suffix.lstrip(".") or "jpg"
        pages.append(
            {
                "page": i,
                "original_url": original,
                "ext": ext.lower(),
                "bare": pixiv_bare_name(illust_id, i, ext),
            }
        )
    return pages


def download_original(
    session: requests.Session,
    original_url: str,
    *,
    illust_id: str,
    timeout: float = 60,
) -> bytes:
    session.headers["Referer"] = f"https://www.pixiv.net/artworks/{illust_id}"
    r = session.get(original_url, timeout=timeout)
    r.raise_for_status()
    data = r.content
    if not data or len(data) < 16:
        raise PixivError("empty image response")
    # Pixiv sometimes returns HTML login/age gate.
    head = data[:200].lstrip().lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html") or head[:1] == b"<":
        raise PixivError(
            "got HTML instead of image — check PHPSESSID / age gate / R-18 login"
        )
    return data


def scan_local_pages(folder: Path, illust_id: str) -> dict[int, Path]:
    """Map Pixiv page index → existing file under ``folder``."""
    folder = Path(folder)
    want = str(illust_id)
    found: dict[int, Path] = {}
    if not folder.is_dir():
        return found
    for path in list_images(folder):
        parsed = parse_pixiv_bare(path.name)
        if not parsed or parsed["illust"] != want:
            continue
        page = parsed["page"]
        prev = found.get(page)
        if prev is None or path.stat().st_size >= prev.stat().st_size:
            found[page] = path
    return found


def find_set_folder(
    store: QueueStore | None,
    pics_root: Path | str,
    illust_id: str,
) -> dict | None:
    """Locate the EH gallery folder that already holds this Pixiv set.

    Prefers DB aliases (sample_path / gallery out_dir), else scans Save-to.
    """
    illust_id = str(illust_id)
    pics_root = Path(pics_root)
    best: dict | None = None

    def consider(
        folder: Path,
        gallery_key: str | None = None,
        source: str = "",
        *,
        allow_empty: bool = False,
    ):
        nonlocal best
        if not folder.is_dir():
            return
        local = scan_local_pages(folder, illust_id)
        n = len(local)
        if n == 0 and not allow_empty:
            return
        row = {
            "folder": folder,
            "gallery_key": gallery_key or "",
            "local_pages": local,
            "local_count": n,
            "source": source,
        }
        if best is None or n > int(best["local_count"]):
            best = row
        elif n == int(best["local_count"]) and gallery_key and not best.get("gallery_key"):
            best = row

    if store is not None:
        # Prefer an already-registered Pixiv gallery folder (may be empty).
        try:
            gkey = pixiv_gallery_key(illust_id)
            gal = store.find_gallery_by_key(gkey) or {}
            od = (gal.get("out_dir") or "").strip()
            if od:
                folder = Path(od)
                if folder.is_dir():
                    consider(folder, gkey, source="pixiv_gallery", allow_empty=True)
                elif not folder.exists():
                    best = {
                        "folder": folder,
                        "gallery_key": gkey,
                        "local_pages": {},
                        "local_count": 0,
                        "source": "pixiv_gallery",
                    }
        except Exception:
            log.exception("pixiv gallery lookup failed for %s", illust_id)

        try:
            cur = store._conn().cursor()
            like = f"%{illust_id}_p%"
            rows = cur.execute(
                """
                SELECT TOP 200 name, bare_name, gallery_key, sample_path
                FROM dbo.image_name_aliases
                WHERE name LIKE ? OR bare_name LIKE ?
                """,
                (like, like),
            ).fetchall()
        except Exception:
            log.exception("alias lookup failed for %s", illust_id)
            rows = []
        by_key: dict[str, Path] = {}
        for name, bare, gkey, sample in rows:
            parsed = parse_pixiv_bare(bare or name or "")
            if not parsed or parsed["illust"] != illust_id:
                continue
            gkey = (gkey or "").strip()
            folder = None
            if sample:
                p = Path(sample)
                if p.is_file():
                    folder = p.parent
                elif p.is_dir():
                    folder = p
            if folder is None and gkey and store is not None:
                gal = store.find_gallery_by_key(gkey) or {}
                od = (gal.get("out_dir") or "").strip()
                if od:
                    folder = Path(od)
            if folder is not None:
                consider(folder, gkey or None, source="db")
                if gkey:
                    by_key.setdefault(gkey, folder)
        for gkey, folder in by_key.items():
            consider(folder, gkey, source="db")

    if pics_root.is_dir():
        for folder in pics_root.iterdir():
            if not folder.is_dir():
                continue
            consider(folder, source="scan")

    if best and store is not None and not best.get("gallery_key"):
        gal = store.find_gallery_by_out_dir(str(best["folder"])) or {}
        if gal.get("gallery_key"):
            best["gallery_key"] = gal["gallery_key"]
    return best


def plan_fill(
    *,
    illust_id: str,
    pages: list[dict],
    local_pages: dict[int, Path],
) -> dict:
    """Compute present / missing page indices against Pixiv page list."""
    remote_pages = {int(p["page"]) for p in pages}
    local_idxs = set(local_pages)
    missing = sorted(remote_pages - local_idxs)
    extra = sorted(local_idxs - remote_pages)
    return {
        "illust_id": illust_id,
        "remote_count": len(pages),
        "local_count": len(local_pages),
        "present": sorted(local_idxs & remote_pages),
        "missing": missing,
        "extra_local": extra,
        "complete": not missing,
    }


def _ext_from_bytes(data: bytes, fallback: str = "jpg") -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return (fallback or "jpg").lower()


def _two_phase_rename(folder: Path, mapping: list[tuple[Path, Path]]) -> int:
    """Rename paths without collisions (temp names, then finals)."""
    if not mapping:
        return 0
    temps: list[tuple[Path, Path]] = []
    renamed = 0
    for i, (src, dest) in enumerate(mapping):
        if src.resolve() == dest.resolve():
            continue
        tmp = folder / f".pixiv_tmp_{i:04d}_{src.name}"
        src.rename(tmp)
        temps.append((tmp, dest))
    for tmp, dest in temps:
        if dest.exists() and dest.resolve() != tmp.resolve():
            dest.unlink()
        tmp.rename(dest)
        renamed += 1
    return renamed


def fill_missing(
    store: QueueStore | None,
    session: requests.Session,
    *,
    illust_id: str,
    folder: Path,
    gallery_key: str | None = None,
    pages: list[dict] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    interval: float = 0.4,
) -> dict:
    """Download missing Pixiv pages into ``folder``, renumber, fingerprint.

    Existing set members keep bytes; only missing pages are fetched. After
    download, all ``{illust}_pN`` files are synth-prefixed by Pixiv page order
    (``01_…_p0``, ``02_…_p1``, …) and registered in the EH DB when ``store``
    is set.
    """
    illust_id = str(illust_id)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stop = should_stop or (lambda: False)
    progress = on_progress or (lambda _m: None)

    if pages is None:
        pages = fetch_illust_pages(session, illust_id)
    local = scan_local_pages(folder, illust_id)
    plan = plan_fill(illust_id=illust_id, pages=pages, local_pages=local)
    saved = 0
    skipped = 0
    failed: list[str] = []

    for page_info in pages:
        if stop():
            break
        page = int(page_info["page"])
        if page in local:
            skipped += 1
            continue
        bare = page_info["bare"]
        progress(f"download {bare}")
        try:
            data = download_original(
                session, page_info["original_url"], illust_id=illust_id
            )
            ext = _ext_from_bytes(data, page_info.get("ext") or "jpg")
            bare = pixiv_bare_name(illust_id, page, ext)
            dest = folder / bare
            # Avoid clobbering unrelated files; use unique temp then rename.
            part = folder / f"{bare}.part"
            part.write_bytes(data)
            if dest.exists():
                dest.unlink()
            part.rename(dest)
            local[page] = dest
            saved += 1
        except Exception as e:
            log.exception("pixiv download failed %s p%s", illust_id, page)
            failed.append(f"p{page}: {e}")
            progress(f"fail {bare}: {e}")
        if interval > 0:
            time.sleep(interval)

    persist_session_cookies(store, session)

    # Renumber all set members by Pixiv page order (keep other gallery files).
    total = len(pages)
    w = index_pad_width(max(total, 1))
    rename_map: list[tuple[Path, Path]] = []
    for page_info in pages:
        page = int(page_info["page"])
        src = local.get(page)
        if src is None or not src.is_file():
            continue
        parsed = parse_pixiv_bare(src.name)
        ext = (parsed or {}).get("ext") or Path(src.name).suffix.lstrip(".") or "jpg"
        bare = pixiv_bare_name(illust_id, page, ext)
        ordered = apply_order_prefix(page + 1, bare, total)
        dest = folder / ordered
        if src.resolve() != dest.resolve():
            rename_map.append((src, dest))
            local[page] = dest
        else:
            local[page] = src

    renamed = _two_phase_rename(folder, rename_map)
    progress(f"renamed {renamed} file(s) to page order")

    fingerprinted = 0
    if store is not None:
        gkey = (gallery_key or "").strip() or None
        if not gkey:
            gal = store.find_gallery_by_out_dir(str(folder)) or {}
            gkey = gal.get("gallery_key")
        for page_info in pages:
            page = int(page_info["page"])
            path = local.get(page)
            if path is None:
                # May have been renamed — resolve by ordered name.
                bare = page_info["bare"]
                ordered = apply_order_prefix(page + 1, bare, total)
                path = folder / ordered
                if not path.is_file():
                    # Try any matching bare under new pad width.
                    for cand in list_images(folder):
                        parsed = parse_pixiv_bare(cand.name)
                        if parsed and parsed["illust"] == illust_id and parsed["page"] == page:
                            path = cand
                            break
            if path is None or not Path(path).is_file():
                continue
            path = Path(path)
            bare = strip_order_prefix(path.name, w)
            if not PIXIV_BARE_RE.match(bare):
                parsed = parse_pixiv_bare(path.name)
                bare = pixiv_bare_name(
                    illust_id, page, (parsed or {}).get("ext") or "jpg"
                )
            digest = sha1_file(path)
            try:
                from image_dhash import compute_dhash

                dh = compute_dhash(path)
            except Exception:
                dh = None
            store.register_sha1(
                digest,
                path.stat().st_size,
                sample_path=str(path),
                gallery_key=gkey,
                dhash=dh,
                name=path.name,
                bare_name=bare,
            )
            try:
                store.enqueue_sha_check(digest)
            except Exception:
                pass
            fingerprinted += 1
        if gkey and total > 0:
            try:
                store.upsert_gallery(
                    gkey,
                    url=pixiv_gallery_url(illust_id)
                    if str(gkey).startswith("pixiv_")
                    else (
                        (store.find_gallery_by_key(gkey) or {}).get("url")
                        or pixiv_gallery_url(illust_id)
                    ),
                    title=(store.find_gallery_by_key(gkey) or {}).get("title"),
                    out_dir=str(folder),
                    image_total=total,
                    saved=saved,
                    skipped=skipped,
                    failed=len(failed),
                )
            except Exception:
                log.exception("upsert pixiv gallery failed")
            try:
                with store._lock:
                    store._conn().cursor().execute(
                        """
                        UPDATE dbo.galleries
                        SET image_total = CASE
                            WHEN image_total IS NULL OR image_total < ? THEN ?
                            ELSE image_total
                        END
                        WHERE gallery_key = ?
                        """,
                        total,
                        total,
                        gkey,
                    )
                    store._conn().commit()
            except Exception:
                log.exception("update gallery image_total failed")
            try:
                gal = store.find_gallery_by_key(gkey)
                if gal and gal.get("url") and not str(gkey).startswith("pixiv_"):
                    store.set_gallery_meta(
                        gal["url"],
                        out_dir=str(folder),
                        image_total=total,
                    )
            except Exception:
                log.exception("update queue gallery meta failed")

    final_local = scan_local_pages(folder, illust_id)
    return {
        "illust_id": illust_id,
        "folder": str(folder),
        "gallery_key": gallery_key or "",
        "remote_count": total,
        "saved": saved,
        "skipped": skipped,
        "failed": failed,
        "renamed": renamed,
        "fingerprinted": fingerprinted,
        "local_count": len(final_local),
        "still_missing": sorted(set(range(total)) - set(final_local)),
        "pad": w,
    }
