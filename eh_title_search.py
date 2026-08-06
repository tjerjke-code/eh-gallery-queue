"""EH title / folder-name search via ``f_search``.

Unquoted long titles often miss (EH ANDs every token). Quoting the folder
name as a phrase is the primary strategy; fallbacks strip trailing bracket
tags and progressively shorten the query.

After title hits, candidates are confirmed against the local folder:
file-count match when complete, else the first few EH thumb names (order
prefixes like ``01_`` / ``001_`` are ignored when comparing).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

from db import _safe_filename
from eh_hash_check import HEADERS, SEARCH_INTERVAL, parse_shash_results, shash_search_url
from logger import get_logger

log = get_logger("eh_title_search")

_MULTI_SPACE = re.compile(r"\s+")
_TRAILING_BRACKET_TAG = re.compile(r"\s*\[[^\]]*\]\s*$")
# Any leading ``NN_`` / ``NNN_`` counter (pad width may differ across copies).
_ORDER_PREFIX = re.compile(r"^(\d+)_(.+)$", re.DOTALL)
_LENGTH_PAGES = re.compile(r"(\d+)\s*pages?", re.IGNORECASE)

VERIFY_SAMPLE_NAMES = 2
VERIFY_MAX_CANDIDATES = 5
# Gallery page fetches (not f_search) — keep a short gap to be polite.
VERIFY_PAGE_INTERVAL = 1.0
# When title search fails: hash this many local files and intersect f_shash hits.
SHA_SAMPLE_COUNT = 3


def normalize_spaces(text: str) -> str:
    return _MULTI_SPACE.sub(" ", (text or "").strip())


def strip_trailing_bracket_tags(name: str) -> str:
    """Drop trailing ``[Chinese]`` / ``[Ongoing]`` / … tags from a folder name."""
    s = (name or "").rstrip()
    while True:
        m = _TRAILING_BRACKET_TAG.search(s)
        if not m:
            break
        s = s[: m.start()].rstrip()
    return s


def quote_phrase(text: str) -> str:
    """Wrap as an EH phrase query (internal quotes removed)."""
    s = normalize_spaces(text).replace('"', " ")
    s = normalize_spaces(s)
    if not s:
        return ""
    return f'"{s}"'


def folder_to_search_queries(folder_name: str) -> list[str]:
    """Ordered ``f_search`` queries, most specific first."""
    raw = normalize_spaces(folder_name)
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q)
            out.append(q)

    add(quote_phrase(raw))

    stripped = strip_trailing_bracket_tags(raw)
    if stripped != raw:
        add(quote_phrase(stripped))

    tokens = normalize_spaces(stripped).split(" ")
    # Drop trailing chunks until a short distinctive core remains.
    while len(tokens) > 4:
        tokens = tokens[:-1]
        add(quote_phrase(" ".join(tokens)))

    # Last resort: unquoted core (circle + first few tokens) if still long.
    if len(tokens) >= 3:
        core = " ".join(tokens[: max(3, min(6, len(tokens)))])
        add(quote_phrase(core))
        add(core)

    return out


def title_search_url(query: str, *, base: str = "https://e-hentai.org/") -> str:
    return f"{base.rstrip('/')}/?f_search={quote_plus(query)}"


def title_similarity(folder_name: str, hit_title: str | None) -> float:
    """Rough 0..1 score for ranking multi-hit results against a folder name."""
    a = normalize_spaces(strip_trailing_bracket_tags(folder_name)).casefold()
    b = normalize_spaces(strip_trailing_bracket_tags(hit_title or "")).casefold()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / max(len(ta), len(tb))


def rank_hits(folder_name: str, hits: list[dict]) -> list[dict]:
    scored = []
    for h in hits:
        row = dict(h)
        row["score"] = title_similarity(folder_name, h.get("title"))
        scored.append(row)
    scored.sort(key=lambda r: (-float(r.get("score") or 0), r.get("gallery_key") or ""))
    return scored


def bare_compare_name(filename: str) -> str:
    """Filename key for matching: drop order prefix + sanitize, casefold."""
    name = (filename or "").strip()
    m = _ORDER_PREFIX.match(name)
    if m:
        name = m.group(2)
    return _safe_filename(name).casefold()


def _looks_like_ban(text: str) -> bool:
    low = (text or "").lower()
    return any(
        s in low
        for s in (
            "temporarily banned",
            "ban expires",
            "exceeded your image",
            "509 bandwidth",
        )
    )


def search_once(
    session: requests.Session,
    query: str,
    *,
    timeout: float = 45,
) -> list[dict]:
    url = title_search_url(query)
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    text = r.text or ""
    if _looks_like_ban(text):
        raise RuntimeError("EH ban / bandwidth limit on title search")
    return parse_shash_results(text, base=str(r.url))


def fetch_gallery_soup(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 45,
) -> BeautifulSoup:
    """GET a gallery page, skipping the content-warning interstitial."""
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    text = r.text or ""
    if _looks_like_ban(text):
        raise RuntimeError("EH ban / bandwidth limit on gallery page")
    soup = BeautifulSoup(text, "lxml")
    h1 = soup.find("h1")
    if h1 and "Content Warning" in h1.get_text():
        wrap = soup.find("p", style="text-align:center")
        a = wrap.find("a", href=True) if wrap else None
        if not a:
            raise RuntimeError("content warning with no continue link")
        r = session.get(a["href"], timeout=timeout)
        r.raise_for_status()
        text = r.text or ""
        if _looks_like_ban(text):
            raise RuntimeError("EH ban / bandwidth limit on gallery page")
        soup = BeautifulSoup(text, "lxml")
    return soup


def gallery_image_total(soup: BeautifulSoup) -> int:
    """Image count from ``gpc`` (“Showing … of N”) or metadata Length."""
    gpc = soup.find("p", class_="gpc")
    if gpc:
        parts = gpc.get_text(strip=True).replace(",", "").split()
        try:
            return int(parts[parts.index("of") + 1])
        except (ValueError, IndexError):
            pass
    gdd = soup.find("div", id="gdd")
    if gdd:
        for tr in gdd.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) >= 2 and cells[0].lower().startswith("length"):
                m = _LENGTH_PAGES.search(cells[1])
                if m:
                    return int(m.group(1))
                digits = re.sub(r"[^\d]", "", cells[1])
                if digits:
                    return int(digits)
    return 0


def gallery_thumb_names(soup: BeautifulSoup, *, limit: int = VERIFY_SAMPLE_NAMES) -> list[str]:
    """First-page thumb display names (EH ``title`` attr after ``Page N: ``)."""
    box = soup.find("div", id="gdt") or soup.find("div", class_="gt200")
    if not box:
        return []
    names: list[str] = []
    for a in box.find_all("a", href=True):
        title_el = a.find(attrs={"title": True})
        if not title_el:
            continue
        name = title_el["title"].split(": ")[-1].strip()
        if name:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def verify_folder_against_meta(
    local_count: int,
    local_name_keys: set[str],
    *,
    image_total: int,
    thumb_names: list[str],
    sample_names: int = VERIFY_SAMPLE_NAMES,
) -> tuple[bool, str]:
    """Return (ok, reason). Count match when complete; else sample thumb names."""
    need = min(sample_names, len(thumb_names)) if thumb_names else 0
    name_hits = 0
    if need:
        for eh_name in thumb_names[:need]:
            if bare_compare_name(eh_name) in local_name_keys:
                name_hits += 1

    if image_total > 0 and local_count == image_total:
        # Full download: count is enough; prefer a free name check when available.
        if need == 0 or name_hits >= 1:
            return True, f"count={image_total}"
        return False, f"count={local_count}=={image_total} but name miss"

    if need > 0 and name_hits == need:
        return True, f"names={name_hits}/{need} (local={local_count} eh={image_total or '?'})"

    if image_total > 0:
        return (
            False,
            f"count {local_count}!={image_total}, names {name_hits}/{need or sample_names}",
        )
    return False, f"no eh total, names {name_hits}/{need or sample_names}"


def local_folder_name_keys(folder: Path) -> tuple[int, set[str]]:
    from local_import import list_images

    files = list_images(Path(folder))
    keys = {bare_compare_name(p.name) for p in files}
    return len(files), keys


def verify_hit_against_folder(
    session: requests.Session,
    folder: Path,
    hit: dict,
    *,
    sample_names: int = VERIFY_SAMPLE_NAMES,
    timeout: float = 45,
) -> dict:
    """Fetch gallery page and confirm ``hit`` against ``folder``. Mutates a copy."""
    folder = Path(folder)
    row = dict(hit)
    local_count, local_keys = local_folder_name_keys(folder)
    soup = fetch_gallery_soup(session, row["url"], timeout=timeout)
    total = gallery_image_total(soup)
    thumbs = gallery_thumb_names(soup, limit=max(sample_names, 1))
    # Prefer title from gallery page when search snippet was empty/short.
    gn = soup.find("h1", id="gn")
    if gn and gn.get_text(strip=True):
        row["title"] = gn.get_text(strip=True)
    ok, reason = verify_folder_against_meta(
        local_count,
        local_keys,
        image_total=total,
        thumb_names=thumbs,
        sample_names=sample_names,
    )
    row["image_total"] = total
    row["verified"] = ok
    row["verify"] = reason
    return row


def confirm_hits(
    session: requests.Session,
    folder: Path,
    hits: list[dict],
    *,
    sample_names: int = VERIFY_SAMPLE_NAMES,
    max_candidates: int = VERIFY_MAX_CANDIDATES,
    interval: float = VERIFY_PAGE_INTERVAL,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """Walk ranked title hits; return the first verified candidate (0..1 items)."""
    stop = should_stop or (lambda: False)
    folder = Path(folder)
    if not folder.is_dir() or not hits:
        return []

    for i, hit in enumerate(hits[:max_candidates]):
        if stop():
            break
        if i > 0 and interval > 0:
            t_end = time.monotonic() + interval
            while time.monotonic() < t_end:
                if stop():
                    return []
                time.sleep(min(0.2, t_end - time.monotonic()))
        try:
            row = verify_hit_against_folder(
                session, folder, hit, sample_names=sample_names
            )
        except Exception as e:
            log.warning(
                "verify failed for %s: %s",
                hit.get("gallery_key") or hit.get("url"),
                e,
            )
            continue
        if row.get("verified"):
            log.info(
                "verified %s — %s (score=%.2f)",
                row.get("gallery_key"),
                row.get("verify"),
                float(row.get("score") or 0),
            )
            return [row]
        log.info(
            "reject %s — %s (score=%.2f)",
            row.get("gallery_key"),
            row.get("verify"),
            float(row.get("score") or 0),
        )
    return []


def pick_sample_files(folder: Path, n: int = SHA_SAMPLE_COUNT) -> list[Path]:
    """Evenly spaced images (first…last) for SHA fallback, natural-sorted."""
    from local_import import list_images, nat_key

    files = sorted(list_images(Path(folder)), key=lambda p: nat_key(p.name))
    if not files:
        return []
    if len(files) <= n:
        return files
    idxs = sorted(
        {int(round(i * (len(files) - 1) / (n - 1))) for i in range(n)}
    )
    return [files[i] for i in idxs]


def shash_once(
    session: requests.Session,
    digest: bytes,
    *,
    timeout: float = 45,
) -> list[dict]:
    url = shash_search_url(digest)
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    text = r.text or ""
    if _looks_like_ban(text):
        raise RuntimeError("EH ban / bandwidth limit on f_shash")
    return parse_shash_results(text, base=str(r.url))


def search_by_sample_shash(
    session: requests.Session,
    folder: Path | str,
    *,
    sample: int = SHA_SAMPLE_COUNT,
    interval: float = SEARCH_INTERVAL,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict]:
    """Hash ~N local files, ``f_shash`` each, return galleries shared by samples.

    Prefer galleries present in **all** sample result sets. Falls back to
    majority (≥2 votes) when the full intersection is empty.
    """
    from local_import import sha1_file

    stop = should_stop or (lambda: False)
    folder = Path(folder)
    files = pick_sample_files(folder, sample)
    if not files:
        return []

    per_keys: list[set[str]] = []
    by_key: dict[str, dict] = {}

    for i, path in enumerate(files):
        if stop():
            return []
        if i > 0 and interval > 0:
            t_end = time.monotonic() + interval
            while time.monotonic() < t_end:
                if stop():
                    return []
                time.sleep(min(0.2, t_end - time.monotonic()))
        try:
            digest = sha1_file(path)
            hits = shash_once(session, digest)
        except Exception as e:
            log.warning("import f_shash sample %s failed: %s", path.name[:50], e)
            per_keys.append(set())
            continue

        keys: set[str] = set()
        for h in hits:
            key = h.get("gallery_key")
            if not key:
                continue
            keys.add(key)
            row = by_key.get(key)
            if row is None:
                row = dict(h)
                row["votes"] = 0
                row["sample_files"] = []
                by_key[key] = row
            row["votes"] = int(row.get("votes") or 0) + 1
            row["sample_files"].append(path.name)
            if h.get("title") and (
                not row.get("title") or len(h["title"]) > len(row.get("title") or "")
            ):
                row["title"] = h["title"]
        per_keys.append(keys)
        log.info(
            "import f_shash sample %s → %s hit(s)",
            path.name[:50],
            len(hits),
        )

    n = len(files)
    nonempty = [k for k in per_keys if k]
    common: set[str] = set.intersection(*nonempty) if len(nonempty) >= 2 else set()
    if n == 1 and nonempty:
        common = set(nonempty[0])

    results: list[dict] = []
    for key, row in by_key.items():
        out = dict(row)
        votes = int(out.get("votes") or 0)
        out["votes"] = votes
        out["vote_of"] = n
        out["in_all_samples"] = key in common
        out["verify"] = f"sha {votes}/{n}"
        out["score"] = votes / max(n, 1)
        out["verified"] = False  # user must confirm
        results.append(out)

    results.sort(
        key=lambda r: (
            -int(bool(r.get("in_all_samples"))),
            -int(r.get("votes") or 0),
            r.get("gallery_key") or "",
        )
    )

    if common:
        return [r for r in results if r.get("gallery_key") in common]
    # Majority: at least 2 sample files agreed (or the only file).
    need = 2 if n >= 2 else 1
    return [r for r in results if int(r.get("votes") or 0) >= need][:5]


def search_by_folder_name(
    session: requests.Session,
    folder_name: str,
    *,
    folder: Path | str | None = None,
    interval: float = SEARCH_INTERVAL,
    should_stop: Callable[[], bool] | None = None,
    verify: bool = True,
) -> tuple[list[dict], str | None]:
    """Try query strategies until hits. Returns (ranked_hits, query_used).

    When ``folder`` is set (and ``verify``), only return a hit confirmed by
    local file count and/or sampled thumb names.
    """
    stop = should_stop or (lambda: False)
    queries = folder_to_search_queries(folder_name)
    last_err: Exception | None = None
    folder_path = Path(folder) if folder else None
    do_verify = bool(verify and folder_path and folder_path.is_dir())

    for i, query in enumerate(queries):
        if stop():
            break
        if i > 0 and interval > 0:
            # Rate-limit between attempts (EH ~1 search / 3s).
            t_end = time.monotonic() + interval
            while time.monotonic() < t_end:
                if stop():
                    return [], None
                time.sleep(min(0.2, t_end - time.monotonic()))
        try:
            hits = search_once(session, query)
        except Exception as e:
            last_err = e
            log.warning("f_search failed for %r: %s", query[:80], e)
            continue
        if not hits:
            continue
        ranked = rank_hits(folder_name, hits)
        log.info(
            "f_search %r → %s hit(s), best score=%.2f",
            query[:80],
            len(ranked),
            float(ranked[0].get("score") or 0),
        )
        if not do_verify:
            return ranked, query
        confirmed = confirm_hits(
            session,
            folder_path,
            ranked,
            interval=VERIFY_PAGE_INTERVAL,
            should_stop=stop,
        )
        if confirmed:
            return confirmed, query
        log.info(
            "f_search %r hits not confirmed for folder %r — trying next query",
            query[:80],
            folder_name[:60],
        )

    if last_err and not queries:
        raise last_err
    return [], None


def default_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s
