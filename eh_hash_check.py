"""Slow EH ``f_shash`` scanner (WishAssistance asset-queue style).

Pending rows live in ``dbo.eh_sha_checks``. A background worker drains them
at ~4s/search, records match galleries, and asks the UI to auto-enqueue
new ``/g/`` URLs at the end of the parse queue.

Ban / bandwidth responses leave the SHA pending and back off (do not mark
``error``) so a temporary limit cannot burn the queue.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from db import QueueStore
from logger import get_logger, log_feed

log = get_logger("eh_hash_check")

HEADERS = {"User-Agent": "Mozilla/5.0"}
SEARCH_INTERVAL = 4.0  # EH search ~1 / 3s; 4s leaves headroom under sustained load
IDLE_SLEEP = 2.0
SEED_BATCH = 2000
# After a search ban, leave jobs pending and wait (exponential, capped).
BAN_COOLDOWN_BASE = 15 * 60  # 15 min
BAN_COOLDOWN_MAX = 2 * 60 * 60  # 2 h
BAN_ERROR_SUBSTR = "ban / bandwidth"

_GALLERY_HREF = re.compile(r"/g/(\d+)/([0-9a-fA-F]+)", re.IGNORECASE)
# Compact/extended search rows append tag chips; ``f:maid`` / ``m:bald`` etc.
_EH_COMPACT_NS_TAG = re.compile(r"\s+[a-z]:\S+")
_EH_META_BRACKET = re.compile(
    r"^\[(?:"
    r"AI[\s_-]?Generated|Chinese|English|Korean|Japanese|Spanish|French|"
    r"Portuguese|Russian|Thai|Vietnamese|German|Italian|Polish|"
    r"Digital|Incomplete|Ongoing|Rework|Colori[sz]ed|Full[\s_-]?Color|"
    r"Factory|Raw|Decensored|Uncensored|Extra|Sample"
    r")\]$",
    re.IGNORECASE,
)
_EH_BARE_TAG_CHIPS = re.compile(
    r"^[a-z][a-z0-9 .'+-]*(?:\s+[a-z][a-z0-9 .'+-]*)*$"
)
_BAN_MARKERS = (
    "temporarily banned",
    "ban expires",
    "exceeded your image",
    "509 bandwidth",
)


def clean_search_hit_title(title: str | None) -> str | None:
    """Gallery name only — drop EH search-result tag chips after the title.

    Search ``<a href=/g/…>`` often wraps ``.glink`` plus tag text like
    ``honkai gakuen elysia f:maid m:bald ai generated``. Folder names use
    the real ``#gn`` title; queue labels should match that.
    """
    s = re.sub(r"\s+", " ", (title or "").strip())
    if not s:
        return None
    m = _EH_COMPACT_NS_TAG.search(s)
    if not m:
        return s
    head = s[: m.start()].rstrip()
    # After a language/meta bracket, EH often lists bare parody/character chips.
    meta = None
    for br in re.finditer(r"\[[^\]]*\]", head):
        if _EH_META_BRACKET.match(br.group(0)):
            meta = br
    if meta is not None:
        after = head[meta.end() :].strip()
        if after and _EH_BARE_TAG_CHIPS.match(after):
            head = head[: meta.end()].rstrip()
    return head or s

OnMatchFn = Callable[[list[dict]], None]
StatusFn = Callable[[str], None]


class EhSearchBan(RuntimeError):
    """EH refused a search (temp ban / bandwidth). Retry later — do not mark error."""


def shash_search_url(digest: bytes) -> str:
    return f"https://e-hentai.org/?f_shash={digest.hex()}"


def parse_shash_results(html: str, *, base: str = "https://e-hentai.org/") -> list[dict]:
    """Extract unique gallery hits from an EH file-search / f_shash page."""
    soup = BeautifulSoup(html or "", "lxml")
    found: list[dict] = []
    seen: set[str] = set()

    # Prefer result table / thumb grid; fall back to any /g/ link.
    roots = []
    itg = soup.find("table", class_="itg")
    if itg:
        roots.append(itg)
    for div in soup.find_all("div", class_=re.compile(r"^gl\d")):
        roots.append(div)
    if not roots:
        roots = [soup]

    for root in roots:
        for a in root.find_all("a", href=True):
            href = a["href"]
            m = _GALLERY_HREF.search(href)
            if not m:
                continue
            key = m.group(1)
            if key in seen:
                continue
            seen.add(key)
            token = m.group(2)
            abs_url = urljoin(base, href).split("#", 1)[0].split("?", 1)[0]
            if not abs_url.rstrip("/").endswith(f"/{token}"):
                abs_url = f"https://e-hentai.org/g/{key}/{token}/"
            # Prefer .glink (real name); fall back to link text then strip tags.
            glink = a.find(class_="glink")
            if glink is not None:
                title = glink.get_text(" ", strip=True) or None
            else:
                title = a.get_text(" ", strip=True) or None
            title = clean_search_hit_title(title)
            if title and len(title) < 2:
                title = None
            found.append(
                {
                    "gallery_key": key,
                    "token": token,
                    "url": abs_url,
                    "title": title,
                }
            )
    return found


class EhHashCheckWorker:
    """Background drain of ``eh_sha_checks`` → auto-queue callbacks."""

    def __init__(
        self,
        store: QueueStore,
        *,
        on_matches: OnMatchFn | None = None,
        on_status: StatusFn | None = None,
        lifecycle_alive: Callable[[], bool] | None = None,
        enabled: Callable[[], bool] | None = None,
        interval: float = SEARCH_INTERVAL,
    ):
        self.store = store
        self.on_matches = on_matches
        self.on_status = on_status
        self.lifecycle_alive = lifecycle_alive or (lambda: True)
        self.enabled = enabled or (lambda: True)
        self.interval = max(3.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        self._ban_strikes = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="eh-hash-check", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 1.5) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=join_timeout)
        self._thread = None
        try:
            self._session.close()
        except Exception:
            pass

    def _emit_status(self, text: str) -> None:
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _loop(self) -> None:
        seeded = False
        log_feed(log, logging.INFO, "EH hash-check worker started")
        try:
            n = self.store.requeue_sha_check_errors(error_substr=BAN_ERROR_SUBSTR)
            if n:
                log_feed(
                    log,
                    logging.INFO,
                    "Requeued %s EH hash check(s) previously failed by ban/limit",
                    n,
                )
        except Exception as e:
            log.exception("requeue_sha_check_errors failed: %s", e)

        while not self._stop.is_set() and self.lifecycle_alive():
            if not self.enabled():
                self._emit_status("EH scan paused")
                self._stop.wait(IDLE_SLEEP)
                continue
            if not seeded:
                try:
                    n = self.store.seed_pending_sha_checks(limit=SEED_BATCH)
                    if n:
                        log_feed(
                            log,
                            logging.INFO,
                            "Seeded %s fingerprint(s) into EH hash-check queue",
                            n,
                        )
                except Exception as e:
                    log.exception("seed_pending_sha_checks failed: %s", e)
                seeded = True
                # Keep seeding in later idle passes if backlog was huge.
            try:
                pending = self.store.count_sha_checks("pending")
            except Exception:
                pending = -1
            job = None
            try:
                job = self.store.claim_next_sha_check()
            except Exception as e:
                log.exception("claim_next_sha_check failed: %s", e)
                self._stop.wait(IDLE_SLEEP)
                continue
            if not job:
                if pending == 0:
                    # More fingerprints may have arrived; re-seed occasionally.
                    try:
                        n = self.store.seed_pending_sha_checks(limit=SEED_BATCH)
                        if n:
                            seeded = True
                            continue
                    except Exception:
                        pass
                self._emit_status(f"EH scan idle (0 pending)")
                self._stop.wait(IDLE_SLEEP)
                continue

            digest = job["sha1"]
            origin = (job.get("gallery_key") or "").strip()
            self._emit_status(
                f"EH scan… {pending} pending — {digest.hex()[:10]}…"
            )
            t0 = time.monotonic()
            ban_hit = False
            try:
                matches = self._search(digest)
                self._ban_strikes = 0
                # Persist all hits; auto-queue filters completed/queued/origin.
                self.store.finish_sha_check(digest, matches=matches)
                to_queue = []
                for m in matches:
                    key = m["gallery_key"]
                    if origin and key == origin:
                        continue
                    url = m["url"]
                    try:
                        if self.store.is_completed(url) or self.store.is_queued(url):
                            continue
                    except Exception:
                        continue
                    to_queue.append(m)
                if to_queue and self.on_matches:
                    try:
                        self.on_matches(to_queue)
                    except Exception:
                        log.exception("on_matches failed")
                if matches:
                    log.info(
                        "f_shash %s → %s hit(s), %s new",
                        digest.hex()[:10],
                        len(matches),
                        len(to_queue),
                    )
                    if to_queue:
                        log_feed(
                            log,
                            logging.INFO,
                            "EH hash %s… found %s new gallery(ies)",
                            digest.hex()[:10],
                            len(to_queue),
                        )
            except EhSearchBan as e:
                # claim_next leaves status=pending — do not finish as error.
                ban_hit = True
                self._ban_strikes = min(self._ban_strikes + 1, 8)
                cooldown = min(
                    BAN_COOLDOWN_MAX,
                    BAN_COOLDOWN_BASE * (2 ** (self._ban_strikes - 1)),
                )
                mins = max(1, int(round(cooldown / 60)))
                log.warning(
                    "f_shash %s: %s — leaving pending, cooldown %sm",
                    digest.hex()[:10],
                    e,
                    mins,
                )
                log_feed(
                    log,
                    logging.WARNING,
                    "EH search ban/limit — pausing hash scan ~%s min (SHA left pending)",
                    mins,
                )
                self._emit_status(f"EH scan paused (ban) ~{mins}m")
                self._stop.wait(cooldown)
            except Exception as e:
                log.warning("f_shash %s failed: %s", digest.hex()[:10], e)
                try:
                    self.store.finish_sha_check(digest, error=str(e))
                except Exception:
                    pass

            if ban_hit or self._stop.is_set():
                continue
            elapsed = time.monotonic() - t0
            wait = max(0.0, self.interval - elapsed)
            if wait > 0:
                self._stop.wait(wait)

        log_feed(log, logging.INFO, "EH hash-check worker stopped")

    def _search(self, digest: bytes) -> list[dict]:
        url = shash_search_url(digest)
        r = self._session.get(url, timeout=45)
        r.raise_for_status()
        text = r.text or ""
        low = text.lower()
        if any(s in low for s in _BAN_MARKERS):
            raise EhSearchBan("EH ban / bandwidth limit on search")
        return parse_shash_results(text, base=str(r.url))
