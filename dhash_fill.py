"""Background dHash fill + incremental near-pair updates."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from image_dhash import (
    DEFAULT_MAX_HAMMING,
    BKTree,
    compute_dhash,
)
from logger import get_logger, log_feed

log = get_logger("dhash_fill")

BATCH = 40
IDLE_SLEEP = 2.0
BUSY_SLEEP = 0.05


class DhashFillWorker:
    """Fill missing ``image_fingerprints.dhash`` and upsert near pairs.

    Keeps an in-memory BK-tree of all known dHashes so each newly filled
    fingerprint can probe neighbors in O(radius search) without a full rebuild.
    """

    def __init__(
        self,
        store,
        *,
        max_hamming: int = DEFAULT_MAX_HAMMING,
        on_progress: Callable[[str], None] | None = None,
        enabled: bool = True,
    ):
        self.store = store
        self.max_hamming = int(max_hamming)
        self.on_progress = on_progress
        self._enabled = threading.Event()
        if enabled:
            self._enabled.set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tree = BKTree()
        self._tree_lock = threading.Lock()
        self._tree_ready = False
        self._gallery_keys: dict[bytes, set[str]] = {}
        self._false_positives: set[tuple[bytes, bytes]] = set()
        self._maps_loaded_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="dhash-fill", daemon=True
        )
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=join_timeout)
        self._thread = None

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._enabled.set()
            self._wake.set()
        else:
            self._enabled.clear()

    def set_max_hamming(self, value: int) -> None:
        self.max_hamming = max(0, int(value))

    def wake(self) -> None:
        self._wake.set()

    def rebuild_tree(self) -> int:
        """Reload BK-tree from DB. Returns fingerprint count."""
        rows = self.store.load_dhash_rows()
        tree = BKTree()
        for sha, dh in rows:
            tree.add(sha, dh)
        with self._tree_lock:
            self._tree = tree
            self._tree_ready = True
        return len(rows)

    def full_rebuild_pairs(self) -> dict:
        """Reload tree + rewrite ``dhash_near_pairs``."""
        n = self.rebuild_tree()
        stats = self.store.rebuild_dhash_near_pairs(max_hamming=self.max_hamming)
        stats["tree"] = n
        return stats

    def _refresh_maps(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._maps_loaded_at and now - self._maps_loaded_at < 60.0:
            return
        self._gallery_keys = self.store.load_sha_gallery_keys()
        self._false_positives = self.store.load_false_positive_pairs()
        self._maps_loaded_at = now

    def _ensure_tree(self) -> None:
        if self._tree_ready:
            return
        self.rebuild_tree()
        self._refresh_maps(force=True)

    def _run(self) -> None:
        log_feed(log, logging.INFO, "dHash fill worker started")
        while not self._stop.is_set():
            if not self._enabled.is_set():
                self._wake.wait(timeout=IDLE_SLEEP)
                self._wake.clear()
                continue
            try:
                self._ensure_tree()
                batch = self.store.list_fingerprints_missing_dhash(limit=BATCH)
                if not batch:
                    if self.on_progress:
                        try:
                            stats = self.store.dhash_fill_stats()
                            self.on_progress(
                                f"dHash fill idle — "
                                f"{stats['filled']}/{stats['total']}"
                            )
                        except Exception:
                            pass
                    self._wake.wait(timeout=IDLE_SLEEP)
                    self._wake.clear()
                    continue

                filled = 0
                failed = 0
                pairs = 0
                for item in batch:
                    if self._stop.is_set() or not self._enabled.is_set():
                        break
                    digest = item["sha1"]
                    paths = self.store.list_paths_for_sha(digest)
                    if not paths and item.get("sample_path"):
                        paths = [item["sample_path"]]
                    dh = None
                    for path in paths:
                        dh = compute_dhash(path)
                        if dh is not None:
                            break
                    if dh is None:
                        failed += 1
                        try:
                            self.store.mark_dhash_failed(digest)
                        except Exception:
                            try:
                                self.store.touch_fingerprint(digest)
                            except Exception:
                                pass
                        continue
                    try:
                        self.store.set_fingerprint_dhash(digest, dh)
                    except Exception as e:
                        failed += 1
                        log.warning("dhash store failed %s: %s", digest.hex()[:12], e)
                        continue
                    filled += 1
                    with self._tree_lock:
                        hits = self._tree.find(
                            dh, radius=self.max_hamming, exclude=digest
                        )
                        self._tree.add(digest, dh)
                    try:
                        if digest not in self._gallery_keys:
                            self._refresh_maps(force=True)
                        else:
                            self._refresh_maps()
                        pairs += self.store.upsert_dhash_near_for_sha(
                            digest,
                            dh,
                            max_hamming=self.max_hamming,
                            tree_hits=hits,
                            gallery_keys=self._gallery_keys,
                            false_positives=self._false_positives,
                        )
                    except Exception as e:
                        log.warning(
                            "near upsert failed %s: %s", digest.hex()[:12], e
                        )
                    time.sleep(BUSY_SLEEP)

                if filled:
                    stats = self.store.dhash_fill_stats()
                    msg = (
                        f"dHash fill +{filled} fail={failed} pairs+={pairs} "
                        f"({stats['filled']}/{stats['total']}"
                        f"{f', skip={stats['failed']}' if stats.get('failed') else ''})"
                    )
                    log_feed(log, logging.INFO, "%s", msg)
                    if self.on_progress:
                        try:
                            self.on_progress(msg)
                        except Exception:
                            pass
                elif failed:
                    # Unreadable leftovers — mark once, do not spin the log.
                    stats = self.store.dhash_fill_stats()
                    msg = (
                        f"dHash fill skipped {failed} unreadable "
                        f"(skip={stats.get('failed', failed)}, "
                        f"{stats['filled']}/{stats['total']})"
                    )
                    log_feed(log, logging.INFO, "%s", msg)
                    if self.on_progress:
                        try:
                            self.on_progress(msg)
                        except Exception:
                            pass
                    self._wake.wait(timeout=IDLE_SLEEP)
                    self._wake.clear()
            except Exception:
                log.exception("dHash fill worker loop error")
                time.sleep(IDLE_SLEEP)
        log.info("dHash fill worker stopped")
