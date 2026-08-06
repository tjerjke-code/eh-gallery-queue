"""EH title / folder-name search via ``f_search``.

Unquoted long titles often miss (EH ANDs every token). Quoting the folder
name as a phrase is the primary strategy; fallbacks strip trailing bracket
tags and progressively shorten the query.
"""

from __future__ import annotations

import re
import time
from typing import Callable
from urllib.parse import quote_plus

import requests

from eh_hash_check import HEADERS, SEARCH_INTERVAL, parse_shash_results
from logger import get_logger

log = get_logger("eh_title_search")

_MULTI_SPACE = re.compile(r"\s+")
_TRAILING_BRACKET_TAG = re.compile(r"\s*\[[^\]]*\]\s*$")


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


def search_by_folder_name(
    session: requests.Session,
    folder_name: str,
    *,
    interval: float = SEARCH_INTERVAL,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[dict], str | None]:
    """Try query strategies until hits. Returns (ranked_hits, query_used)."""
    stop = should_stop or (lambda: False)
    queries = folder_to_search_queries(folder_name)
    last_err: Exception | None = None

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
        if hits:
            ranked = rank_hits(folder_name, hits)
            log.info(
                "f_search %r → %s hit(s), best score=%.2f",
                query[:80],
                len(ranked),
                float(ranked[0].get("score") or 0),
            )
            return ranked, query

    if last_err and not queries:
        raise last_err
    return [], None


def default_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s
