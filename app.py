"""EH Gallery Queue UI + downloader (hot-reloadable).

Owned by ``eh_gallery_queue.HotReloadShell``. Ctrl+R reloads this module
and rebuilds :class:`App` without killing the process.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from pathlib import Path

import requests
import tkinter as tk
from bs4 import BeautifulSoup
from tkinter import filedialog, messagebox, scrolledtext, ttk

from db import (
    QueueStore,
    gallery_key_from_url,
    index_pad_width,
    normalize_image_links,
    strip_order_prefix,
)
from eh_hash_check import EhHashCheckWorker, SEARCH_INTERVAL, clean_search_hit_title
from eh_title_search import (
    default_session,
    search_by_folder_name,
    search_by_sample_shash,
    verify_hit_against_folder,
)
from dhash_fill import DhashFillWorker
from fs_links import (
    ensure_symlink,
    move_real_file,
    remove_path_if_link_or_dup,
    resolve_real_file,
    same_entry,
    same_path,
    strip_peer_presence,
)
from image_dhash import DEFAULT_MAX_HAMMING, compute_dhash
from local_import import (
    extract_toplevel_archives,
    import_local_gallery,
    list_images,
    nat_key,
    scan_gallery_folders,
)
from logger import get_logger, log_feed

log = get_logger('app')

DEFAULT_DIR = r'a:\trt\.Pics'
HEADERS = {'User-Agent': 'Mozilla/5.0'}
REQUEST_INTERVAL = 0.35
MAX_RETRIES = 5
DEFAULT_WORKERS = 4
WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Listbox colors: manual (default) vs EH-discovered auto-queue vs currently parsing.
AUTO_QUEUE_FG = '#1565c0'
MANUAL_QUEUE_FG = '#000000'
RUNNING_QUEUE_FG = '#2e7d32'
# Duped hover filmstrip: this gallery vs peer.
DUPED_COLOR_LEFT = '#2B6CB0'
DUPED_COLOR_RIGHT = '#C05621'
DUPED_COLOR_FOCUS = '#ECC94B'
DUPED_NEIGHBOR_RADIUS = 3
DUPED_THUMB_SIZE = 86
DUPED_COMPARE_SIZE = 220
DUPED_BOARD_THUMB_W = 56
DUPED_BOARD_THUMB_H = 80
DUPED_BOARD_BATCH = 2
DUPED_COMPARE_WIN_W = 520
DUPED_COMPARE_WIN_H = 700
DUPED_MANUAL_HAMMING = 255
# Cross-thread UI: workers never call Tk/after (Tcl lock stall on Windows).
UI_DRAIN_MS = 50
UI_DRAIN_BATCH = 40
DUPED_THUMB_APPLY_PER_TICK = 12
DUPED_THUMB_READY_MAX = 240
DUPED_TREE_CHUNK = 60


class DownloadStopped(Exception):
    pass


class RateLimiter:
    """Serialize request *starts* so N workers don't stampede the site."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def sanitize_name(name: str) -> str:
    name = WIN_BAD.sub('_', name).strip(' .')
    return name[:180] or 'gallery'


def looks_like_ban(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in (
        'temporarily banned',
        'ban expires',
        'exceeded your image',
        '509 bandwidth',
        'owner has enabled',
    ))


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 16:
        return False
    if data[:1] == b'<' or data[:15].lstrip().lower().startswith(b'<!doctype') or data[:5].lower() == b'<html':
        return False
    return (
        data[:3] == b'\xff\xd8\xff'
        or data[:8] == b'\x89PNG\r\n\x1a\n'
        or data[:6] in (b'GIF87a', b'GIF89a')
        or data[:4] == b'RIFF'
    )


def _sha1_bytes(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _sha1_file(path: Path, chunk: int = 1024 * 1024) -> bytes:
    h = hashlib.sha1()
    with path.open('rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.digest()


def find_existing_image(
    target_dir: Path, ordered_name: str, *, total: int
) -> Path | None:
    """Find an on-disk file for this slot (ordered name or legacy bare thumb name)."""
    target_dir = Path(target_dir)
    direct = target_dir / ordered_name
    if direct.is_file() and direct.stat().st_size > 0:
        return direct
    w = index_pad_width(total)
    bare = strip_order_prefix(ordered_name, w)
    if bare and bare != ordered_name:
        legacy = target_dir / bare
        if legacy.is_file() and legacy.stat().st_size > 0:
            return legacy
    return None


def adopt_to_ordered_name(existing: Path, ordered_name: str) -> Path:
    """Rename legacy bare file to ordered name when needed."""
    dest = existing.parent / ordered_name
    if existing.resolve() == dest.resolve():
        return dest
    if dest.exists():
        return dest
    existing.rename(dest)
    return dest


class EHDownloader:
    def __init__(
        self,
        out_dir: str,
        log,
        should_stop,
        workers=DEFAULT_WORKERS,
        interval=REQUEST_INTERVAL,
        store: QueueStore | None = None,
        gallery_url: str | None = None,
        on_meta=None,
    ):
        self.out_dir = out_dir
        self.log = log
        self.should_stop = should_stop
        self.workers = max(1, int(workers))
        self.limiter = RateLimiter(interval)
        self._local = threading.local()
        self._stats_lock = threading.Lock()
        self.store = store
        self.gallery_url = gallery_url
        self.on_meta = on_meta
        self._name_total = 1
        self._dhash_worker = None  # App may attach DhashFillWorker for wake()

    def _session(self):
        s = getattr(self._local, 'session', None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            self._local.session = s
        return s

    def check_stop(self):
        if self.should_stop():
            raise DownloadStopped()

    def get(self, url, binary=False, retries=MAX_RETRIES):
        last_err = None
        for attempt in range(1, retries + 1):
            self.check_stop()
            try:
                self.limiter.wait()
                self.check_stop()
                r = self._session().get(url, timeout=30)
                r.raise_for_status()
                body = r.content if binary else r.text
                if not binary and isinstance(body, str) and looks_like_ban(body):
                    raise RuntimeError('rate-limit / ban page')
                if binary and not looks_like_image(body):
                    text = body[:500].decode('utf-8', 'ignore')
                    if looks_like_ban(text) or '<html' in text.lower():
                        raise RuntimeError('non-image / ban response')
                return body
            except DownloadStopped:
                raise
            except Exception as e:
                last_err = e
                self.log(f'  request fail ({attempt}/{retries}): {e}')
                time.sleep(REQUEST_INTERVAL * attempt)
        raise RuntimeError(f'Failed after {retries} tries: {last_err}')

    def skip_warning(self, url):
        source = self.get(url)
        soup = BeautifulSoup(source, 'lxml')
        h1 = soup.find('h1')
        if h1 and 'Content Warning' in h1.text:
            url = soup.find('p', style='text-align:center').find('a')['href']
            source = self.get(url)
            soup = BeautifulSoup(source, 'lxml')
        return url, soup

    def page_count(self, soup):
        ptt = soup.find('table', class_='ptt')
        if ptt:
            nums = [int(td.get_text(strip=True)) for td in ptt.find_all('td')
                    if td.get_text(strip=True).isdigit()]
            if nums:
                return max(nums)
        gpc = soup.find('p', class_='gpc')
        if gpc:
            parts = gpc.get_text(strip=True).replace(',', '').split()
            try:
                total = int(parts[parts.index('of') + 1])
                per_page = int(parts[parts.index('-') + 1])
                return max(1, ceil(total / per_page))
            except (ValueError, IndexError):
                pass
        return 1

    def image_total(self, soup):
        gpc = soup.find('p', class_='gpc')
        if gpc:
            parts = gpc.get_text(strip=True).replace(',', '').split()
            try:
                return int(parts[parts.index('of') + 1])
            except (ValueError, IndexError):
                pass
        return 0

    def thumb_links(self, soup):
        box = soup.find('div', id='gdt') or soup.find('div', class_='gt200')
        if not box:
            return []
        links = []
        for a in box.find_all('a', href=True):
            title_el = a.find(attrs={'title': True})
            if not title_el:
                continue
            name = title_el['title'].split(': ')[-1]
            links.append((a['href'], name))
        return links

    def resolve_img(self, pp_url):
        source = self.get(pp_url)
        soup = BeautifulSoup(source, 'lxml')
        img = soup.find('img', id='img')
        if img is not None:
            return img['src']

        loadfail = soup.find(id='loadfail')
        if loadfail and loadfail.get('onclick'):
            m = re.search(r"nl\('([^']+)'\)", loadfail['onclick'])
            if m:
                sep = '&' if '?' in pp_url else '?'
                source = self.get(f'{pp_url}{sep}nl={m.group(1)}')
                soup = BeautifulSoup(source, 'lxml')
                img = soup.find('img', id='img')
                if img is not None:
                    return img['src']
        raise RuntimeError('no #img (rate-limit / ban / bad page)')

    def _fp_register(
        self,
        digest: bytes,
        byte_len: int,
        *,
        path: str | Path | None = None,
        ordered_name: str | None = None,
    ) -> None:
        if not self.store:
            return
        name = ordered_name or (Path(path).name if path else None)
        bare = None
        if name:
            bare = strip_order_prefix(name, index_pad_width(self._name_total))
        dhash = None
        if path:
            try:
                dhash = compute_dhash(path)
            except Exception:
                dhash = None
        try:
            self.store.register_sha1(
                digest,
                byte_len,
                sample_path=str(path) if path else None,
                gallery_key=gallery_key_from_url(self.gallery_url or ''),
                dhash=dhash,
                name=name,
                bare_name=bare,
            )
        except Exception as e:
            self.log(f'  fp register failed: {e}')
        # App owns the fill worker; EHDownloader must not assume the attribute.
        worker = getattr(self, '_dhash_worker', None)
        if dhash is not None and worker is not None:
            try:
                worker.wake()
            except Exception:
                pass

    def _bump(self, stats, key, pic_name=None, error=None):
        with self._stats_lock:
            stats[key] += 1
            done = stats['saved'] + stats['skipped'] + stats['failed']
            total = stats['total']
        if self.store and self.gallery_url and pic_name:
            try:
                self.store.update_image(
                    self.gallery_url,
                    pic_name,
                    key,
                    error=str(error) if error else None,
                    bump=key,
                )
            except Exception as e:
                self.log(f'  db image update failed: {e}')
        if key == 'saved' and pic_name:
            self.log(f'  saved {pic_name}  ({done}/{total})')
        elif key == 'failed' and pic_name:
            self.log(f'  SKIPPED {pic_name}')

    def download_image(self, pp_url, pic_name, target_dir, stats):
        path = Path(target_dir) / pic_name
        existing = find_existing_image(
            Path(target_dir), pic_name, total=self._name_total
        )
        if existing is not None:
            try:
                path = adopt_to_ordered_name(existing, pic_name)
            except OSError as e:
                self.log(f'  rename {existing.name} → {pic_name} failed: {e}')
                path = existing
            if self.store:
                try:
                    digest = _sha1_file(path)
                    self._fp_register(
                        digest,
                        path.stat().st_size,
                        path=path,
                        ordered_name=pic_name,
                    )
                except Exception as e:
                    self.log(f'  fp backfill failed: {e}')
            self._bump(stats, 'skipped', pic_name)
            return

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            self.check_stop()
            try:
                img_url = self.resolve_img(pp_url)
                data = self.get(img_url, binary=True)
                if not looks_like_image(data):
                    raise RuntimeError('invalid image data')
                digest = _sha1_bytes(data)
                if self.store:
                    try:
                        hit = self.store.lookup_sha1(digest)
                    except Exception as e:
                        hit = None
                        self.log(f'  fp lookup failed: {e}')
                    if hit:
                        prior = hit.get('sample_path') or 'known file'
                        aliases = []
                        try:
                            aliases = self.store.list_name_aliases(digest)
                        except Exception:
                            pass
                        alias_hint = ''
                        if aliases:
                            names = sorted({
                                a.get('bare_name') or a.get('name')
                                for a in aliases
                                if a.get('bare_name') or a.get('name')
                            })
                            if names:
                                alias_hint = f'; known as {names[:3]}'
                        self.log(
                            f'  skip {pic_name} — exact match in DB '
                            f'({prior}){alias_hint}'
                        )
                        self._fp_register(
                            digest,
                            len(data),
                            path=hit.get('sample_path'),
                            ordered_name=pic_name,
                        )
                        # Visibility: symlink into this gallery (DB is still
                        # source of truth; pair SHA never re-enqueues EH check).
                        real = resolve_real_file(hit.get('sample_path'))
                        if real is not None and not same_path(real, path):
                            link_status = ensure_symlink(path, real)
                            if link_status == 'ok':
                                self.log(f'  link {pic_name} → {real.name}')
                                if self.store:
                                    try:
                                        bare = strip_order_prefix(
                                            pic_name,
                                            index_pad_width(self._name_total),
                                        )
                                        self.store.record_name_alias(
                                            digest,
                                            name=pic_name,
                                            bare_name=bare,
                                            gallery_key=gallery_key_from_url(
                                                self.gallery_url or ''
                                            ),
                                            sample_path=str(path),
                                        )
                                    except Exception:
                                        pass
                            elif link_status == 'failed':
                                self.log(
                                    f'  symlink failed for {pic_name} '
                                    f'(enable Windows Developer Mode?)'
                                )
                        self._bump(stats, 'skipped', pic_name)
                        return
                part = path.with_suffix(path.suffix + '.part')
                part.write_bytes(data)
                part.replace(path)
                self._fp_register(
                    digest,
                    len(data),
                    path=path,
                    ordered_name=pic_name,
                )
                self._bump(stats, 'saved', pic_name)
                return
            except DownloadStopped:
                raise
            except Exception as e:
                last_err = e
                self.log(f'  {pic_name}: {e} — retry {attempt}/{MAX_RETRIES}')
                time.sleep(REQUEST_INTERVAL * attempt)
        self._bump(stats, 'failed', pic_name, error=last_err)
        self.log(f'  reason: {last_err}')

    def _fetch_page_links(self, gallery_url, page_index, pages):
        self.check_stop()
        source = self.get(f'{gallery_url}?p={page_index}')
        links = self.thumb_links(BeautifulSoup(source, 'lxml'))
        if not links:
            time.sleep(REQUEST_INTERVAL * 2)
            source = self.get(f'{gallery_url}?p={page_index}')
            links = self.thumb_links(BeautifulSoup(source, 'lxml'))
        if links:
            self.log(f'  page {page_index + 1}/{pages}: {len(links)} thumbs')
        else:
            self.log(f'  page {page_index + 1}/{pages}: empty — skip')
        return links

    def collect_links(self, gallery_url, pages):
        """Fetch gallery index pages in parallel, keep page order."""
        by_page = {}
        page_workers = min(self.workers, pages)
        with ThreadPoolExecutor(max_workers=page_workers) as pool:
            futs = {
                pool.submit(self._fetch_page_links, gallery_url, n, pages): n
                for n in range(pages)
            }
            try:
                for fut in as_completed(futs):
                    self.check_stop()
                    n = futs[fut]
                    try:
                        by_page[n] = fut.result()
                    except DownloadStopped:
                        for f in futs:
                            f.cancel()
                        raise
                    except Exception as e:
                        self.log(f'  page {n + 1}: {e}')
                        by_page[n] = []
            except DownloadStopped:
                pool.shutdown(wait=False, cancel_futures=True)
                raise

        links = []
        for n in range(pages):
            links.extend(by_page.get(n) or [])
        return links

    def parse_gallery(self, url):
        self.check_stop()
        self.gallery_url = url.strip()
        url, soup = self.skip_warning(self.gallery_url)
        title_raw = soup.find('h1', id='gn').text
        title = sanitize_name(title_raw)
        save_root = Path(self.out_dir)
        target_dir = save_root / title
        # Import enqueue stores the existing gallery folder on the queue row.
        # mark_running() may temporarily set out_dir to Save-to root — ignore that.
        if self.store:
            try:
                key = gallery_key_from_url(self.gallery_url)
                prior = None
                if key:
                    q = self.store.find_queue_by_key(key)
                    prior = (q or {}).get('out_dir')
                if prior:
                    p = Path(prior)
                    try:
                        is_save_root = p.resolve() == save_root.resolve()
                    except OSError:
                        is_save_root = (
                            str(p).rstrip('\\/').casefold()
                            == str(save_root).rstrip('\\/').casefold()
                        )
                    if p.is_dir() and not is_save_root:
                        target_dir = p
            except Exception:
                pass
        target_dir.mkdir(parents=True, exist_ok=True)

        pages = self.page_count(soup)
        total = self.image_total(soup)
        stats = {'saved': 0, 'skipped': 0, 'failed': 0, 'total': total or 0}
        self.log(f'— {title_raw}')
        self.log(f'  {pages} pages, ~{total} images, workers={self.workers} → {target_dir}')

        if self.store:
            try:
                self.store.set_gallery_meta(
                    self.gallery_url,
                    title=title_raw[:256],
                    out_dir=str(target_dir),
                    image_total=total or None,
                )
            except Exception as e:
                self.log(f'  db meta update failed: {e}')
        if self.on_meta:
            try:
                self.on_meta(title=title_raw, image_total=total or None)
            except Exception:
                pass

        all_links = normalize_image_links(
            self.collect_links(url, pages),
            total=total or None,
        )
        self._name_total = max(len(all_links), int(total or 0), 1)
        self.log(f'  collected {len(all_links)} image pages')
        stats['total'] = len(all_links) or stats['total']

        if self.store:
            try:
                self.store.replace_images(self.gallery_url, all_links)
            except Exception as e:
                self.log(f'  db image list failed: {e}')

        # Fast skip / adopt legacy names (no network)
        todo = []
        for pp_url, pic_name in all_links:
            existing = find_existing_image(
                target_dir, pic_name, total=self._name_total
            )
            if existing is not None:
                try:
                    adopted = adopt_to_ordered_name(existing, pic_name)
                except OSError as e:
                    self.log(f'  rename {existing.name} → {pic_name} failed: {e}')
                    adopted = existing
                if self.store:
                    try:
                        digest = _sha1_file(adopted)
                        bare = strip_order_prefix(
                            pic_name, index_pad_width(self._name_total)
                        )
                        self.store.register_sha1(
                            digest,
                            adopted.stat().st_size,
                            sample_path=str(adopted),
                            gallery_key=gallery_key_from_url(self.gallery_url),
                            name=pic_name,
                            bare_name=bare,
                        )
                    except Exception as e:
                        self.log(f'  fp backfill failed: {e}')
                self._bump(stats, 'skipped', pic_name)
            else:
                todo.append((pp_url, pic_name))

        if stats['skipped']:
            self.log(f'  already have {stats["skipped"]}, downloading {len(todo)}')

        if todo:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futs = [
                    pool.submit(self.download_image, pp, name, target_dir, stats)
                    for pp, name in todo
                ]
                try:
                    for fut in as_completed(futs):
                        self.check_stop()
                        try:
                            fut.result()
                        except DownloadStopped:
                            for f in futs:
                                f.cancel()
                            pool.shutdown(wait=False, cancel_futures=True)
                            raise
                        except Exception as e:
                            self.log(f'  worker error: {e}')
                except DownloadStopped:
                    raise

        self.log(
            f"  done: saved {stats['saved']}, skipped {stats['skipped']}, "
            f"failed {stats['failed']}"
        )
        stats['title'] = title_raw
        stats['target_dir'] = str(target_dir)
        return stats


class App(ttk.Frame):
    """Main UI frame — destroyed/rebuilt on Ctrl+R by the shell."""

    def __init__(self, master, store: QueueStore | None = None):
        super().__init__(master)
        self.store = store
        self._lifecycle_alive = True

        self.job_queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = None
        self._queue_urls = []
        self._queue_sources: dict[str, str] = {}
        self._queue_titles: dict[str, str] = {}
        self._queue_totals: dict[str, int | None] = {}
        self._queue_view: list[str] = []
        self._queue_filter_raw = ''
        self._current_url = None
        self._hash_worker: EhHashCheckWorker | None = None
        self._eh_scan_status = ''
        # Plain bool — hash worker must not read BooleanVar (Tcl lock).
        self._eh_scan_enabled = True
        self._ui_pending: deque[tuple[object, tuple]] = deque()
        self._ui_pending_lock = threading.Lock()
        self._ui_drain_alive = True
        self._import_rows: dict[str, dict] = {}
        self._import_stop = threading.Event()
        self._import_busy = False
        self._duped_rows: dict[str, dict] = {}
        self._duped_files: dict[str, dict] = {}
        self._duped_stop = threading.Event()
        self._duped_busy = False
        self._duped_preview: tk.Toplevel | None = None
        self._duped_preview_photos: list = []
        self._duped_preview_iid: str | None = None
        self._duped_preview_after: str | None = None
        self._duped_preview_path: str | None = None
        self._duped_gallery_sort: tuple[str, bool] = ('shared', True)  # col, reverse
        self._duped_file_sort: tuple[str, bool] = ('name', False)
        self._duped_focus_key: str | None = None
        self._duped_seq_cache: dict[str, list[dict]] = {}
        self._duped_preview_ctx: dict | None = None
        self._duped_mode = 'exact'  # exact | near
        self._dhash_worker: DhashFillWorker | None = None
        self._duped_compare_photos: list = []
        self._duped_board_items: list[dict] = []
        self._duped_board_loaded = 0
        self._duped_linked_pairs: set[tuple[bytes, bytes]] = set()
        self._duped_board_canvas: tk.Canvas | None = None
        self._duped_board_window = None
        self._duped_board_loading = False
        self._duped_thumb_cache: dict[str, object] = {}
        self._duped_thumb_gen = 0
        self._duped_thumb_queue: queue.Queue = queue.Queue()
        self._duped_thumb_thread: threading.Thread | None = None
        self._duped_thumb_ready: deque = deque()
        self._duped_thumb_ready_lock = threading.Lock()
        self._duped_file_pop_gen = 0
        self._duped_compare_win: tk.Toplevel | None = None
        self._duped_compare_idx = 0
        self._duped_compare_win_photos: list = []
        self._duped_cw_widgets: dict = {}
        self._duped_compare_reopen_after_refresh: int | None = None

        # --- shared: Save to ---
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')

        ttk.Label(top, text='Save to:').pack(side='left')
        self.dir_var = tk.StringVar(value=DEFAULT_DIR)
        ttk.Entry(top, textvariable=self.dir_var).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(top, text='Browse…', command=self.browse_dir).pack(side='left')

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=4, pady=(0, 4))
        self._notebook = nb

        queue_tab = ttk.Frame(nb)
        import_tab = ttk.Frame(nb)
        duped_tab = ttk.Frame(nb)
        nb.add(queue_tab, text='Queue')
        nb.add(import_tab, text='Import')
        nb.add(duped_tab, text='Duped')

        self._build_queue_tab(queue_tab)
        self._build_import_tab(import_tab)
        self._build_duped_tab(duped_tab)

        self.status = tk.StringVar(value='Idle')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')

        self._hydrate_from_store()
        self._start_ui_drain()
        self._start_hash_worker()
        self._start_dhash_worker()
        log_feed(log, logging.INFO, 'UI ready')

    def _build_queue_tab(self, parent: ttk.Frame):
        opts = ttk.Frame(parent, padding=(8, 4))
        opts.pack(fill='x')
        ttk.Label(opts, text='Workers:').pack(side='left')
        self.workers_var = tk.IntVar(value=DEFAULT_WORKERS)
        ttk.Spinbox(opts, from_=1, to=8, width=4, textvariable=self.workers_var).pack(
            side='left', padx=4
        )
        ttk.Label(opts, text='(3–4 safe · higher = faster, more ban risk)').pack(side='left')
        self.eh_scan_var = tk.BooleanVar(value=True)
        self._eh_scan_enabled = True
        ttk.Checkbutton(
            opts,
            text='EH dupe scan',
            variable=self.eh_scan_var,
            command=self._on_eh_scan_toggle,
        ).pack(side='right')

        add_row = ttk.Frame(parent, padding=(8, 4))
        add_row.pack(fill='x')
        ttk.Label(add_row, text='URL:').pack(side='left')
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(add_row, textvariable=self.url_var)
        self.url_entry.pack(side='left', fill='x', expand=True, padx=4)
        self.url_entry.bind('<Return>', lambda e: self.add_url())
        ttk.Button(add_row, text='Add to queue', command=self.add_url).pack(side='left')

        mid = ttk.Frame(parent, padding=8)
        mid.pack(fill='both', expand=True)

        left = ttk.Frame(mid)
        left.pack(side='left', fill='both', expand=True)
        q_head = ttk.Frame(left)
        q_head.pack(fill='x')
        ttk.Label(q_head, text='Parse queue').pack(side='left')
        ttk.Label(
            q_head,
            text='(blue = auto from EH hash)',
            foreground=AUTO_QUEUE_FG,
        ).pack(side='right')

        filt = ttk.Frame(left)
        filt.pack(fill='x', pady=(2, 4))
        ttk.Label(filt, text='Filter:').pack(side='left')
        self.queue_filter_var = tk.StringVar()
        self.queue_filter_entry = ttk.Entry(filt, textvariable=self.queue_filter_var)
        self.queue_filter_entry.pack(side='left', fill='x', expand=True, padx=4)
        self.queue_filter_var.trace_add('write', lambda *_: self._on_queue_filter_changed())
        ttk.Button(filt, text='Clear', width=6, command=self._clear_queue_filter).pack(
            side='left'
        )
        self.queue_filter_hint = ttk.Label(
            left,
            text='name / url · images:42 · images:>=40  ·  ▶ green = parsing (right-click → Copy URL)',
            foreground='#666666',
        )
        self.queue_filter_hint.pack(anchor='w')

        self.listbox = tk.Listbox(left, height=10, selectmode='extended')
        self.listbox.pack(fill='both', expand=True)
        self.listbox.bind('<Button-3>', self._on_queue_context)
        self._queue_menu = tk.Menu(self, tearoff=0)

        btns = ttk.Frame(left)
        btns.pack(fill='x', pady=4)
        ttk.Button(btns, text='Remove', command=self.remove_selected).pack(side='left')
        ttk.Button(btns, text='Clear', command=self.clear_queue).pack(side='left', padx=4)
        ttk.Button(btns, text='↑', width=3, command=lambda: self.queue_move(-1)).pack(
            side='left', padx=(12, 0)
        )
        ttk.Button(btns, text='↓', width=3, command=lambda: self.queue_move(1)).pack(
            side='left', padx=2
        )
        ttk.Button(btns, text='Top', command=lambda: self.queue_move('top')).pack(
            side='left', padx=(8, 0)
        )
        ttk.Button(btns, text='Bottom', command=lambda: self.queue_move('bottom')).pack(
            side='left', padx=2
        )

        right = ttk.Frame(mid)
        right.pack(side='left', fill='y', padx=(8, 0))
        self.start_btn = ttk.Button(right, text='Start', command=self.start)
        self.start_btn.pack(fill='x', pady=2)
        self.stop_btn = ttk.Button(right, text='Stop', command=self.stop, state='disabled')
        self.stop_btn.pack(fill='x', pady=2)
        ttk.Button(right, text='Reload (Ctrl+R)', command=self._request_reload).pack(
            fill='x', pady=(12, 2)
        )

        ttk.Label(parent, text='Log', padding=(8, 0)).pack(anchor='w')
        self.log_box = scrolledtext.ScrolledText(
            parent, height=12, state='disabled', wrap='word'
        )
        self.log_box.pack(fill='both', expand=True, padx=8, pady=(0, 8))

    def _build_import_tab(self, parent: ttk.Frame):
        help_row = ttk.Frame(parent, padding=(8, 6))
        help_row.pack(fill='x')
        ttk.Label(
            help_row,
            text='Scan Save-to → search EH by title → auto-queue matches (or Import into DB)',
        ).pack(side='left')

        tools = ttk.Frame(parent, padding=(8, 0))
        tools.pack(fill='x')
        ttk.Button(tools, text='Scan', command=self.import_scan).pack(side='left')
        ttk.Button(
            tools, text='Search unmatched', command=self.import_search_unmatched
        ).pack(side='left', padx=4)
        ttk.Button(
            tools, text='Search selected', command=self.import_search_selected
        ).pack(side='left')
        ttk.Button(tools, text='Stop search', command=self.import_stop_search).pack(
            side='left', padx=4
        )
        ttk.Button(
            tools, text='Import selected', command=self.import_selected
        ).pack(side='left', padx=(12, 0))
        ttk.Button(
            tools, text='Enqueue selected', command=self.import_enqueue_selected
        ).pack(side='left', padx=4)

        cols = ('folder', 'files', 'db', 'queue', 'match', 'score')
        tree_frame = ttk.Frame(parent, padding=8)
        tree_frame.pack(fill='both', expand=True)
        self.import_tree = ttk.Treeview(
            tree_frame,
            columns=cols,
            show='headings',
            selectmode='extended',
            height=12,
        )
        self.import_tree.heading('folder', text='Folder')
        self.import_tree.heading('files', text='Files')
        self.import_tree.heading('db', text='DB')
        self.import_tree.heading('queue', text='Queue')
        self.import_tree.heading('match', text='EH match')
        self.import_tree.heading('score', text='Score')
        self.import_tree.column('folder', width=320, stretch=True)
        self.import_tree.column('files', width=50, stretch=False, anchor='center')
        self.import_tree.column('db', width=70, stretch=False, anchor='center')
        self.import_tree.column('queue', width=70, stretch=False, anchor='center')
        self.import_tree.column('match', width=280, stretch=True)
        self.import_tree.column('score', width=50, stretch=False, anchor='center')
        yscroll = ttk.Scrollbar(
            tree_frame, orient='vertical', command=self.import_tree.yview
        )
        self.import_tree.configure(yscrollcommand=yscroll.set)
        self.import_tree.pack(side='left', fill='both', expand=True)
        yscroll.pack(side='right', fill='y')
        self.import_tree.bind('<<TreeviewSelect>>', self._on_import_select)

        ov = ttk.Frame(parent, padding=(8, 4))
        ov.pack(fill='x')
        ttk.Label(ov, text='Override URL:').pack(side='left')
        self.import_url_var = tk.StringVar()
        self.import_url_entry = ttk.Entry(ov, textvariable=self.import_url_var)
        self.import_url_entry.pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(ov, text='Apply to selected', command=self.import_apply_url).pack(
            side='left'
        )

        self.import_status = tk.StringVar(
            value='Scan Save-to to list local galleries (folders + top-level archives).'
        )
        ttk.Label(parent, textvariable=self.import_status, padding=(8, 4)).pack(fill='x')

    def _build_duped_tab(self, parent: ttk.Frame):
        help_row = ttk.Frame(parent, padding=(8, 6))
        help_row.pack(fill='x')
        ttk.Label(
            help_row,
            text=(
                'Exact = SHA-1. Near = dHash. Match board = index (\u00b13). '
                'Double-click row/card \u2192 large compare. Right-click \u2192 Explorer.'
            ),
        ).pack(side='left')

        tools = ttk.Frame(parent, padding=(8, 0))
        tools.pack(fill='x')
        ttk.Button(tools, text='Refresh', command=self.duped_refresh).pack(side='left')

        self.duped_mode_var = tk.StringVar(value='exact')
        ttk.Radiobutton(
            tools,
            text='Exact SHA',
            value='exact',
            variable=self.duped_mode_var,
            command=self._on_duped_mode_change,
        ).pack(side='left', padx=(12, 0))
        ttk.Radiobutton(
            tools,
            text='Near dHash',
            value='near',
            variable=self.duped_mode_var,
            command=self._on_duped_mode_change,
        ).pack(side='left', padx=(4, 0))

        self.duped_undecided_var = tk.BooleanVar(value=True)
        self.duped_undecided_chk = ttk.Checkbutton(
            tools,
            text='Undecided only',
            variable=self.duped_undecided_var,
            command=self.duped_refresh,
        )
        self.duped_undecided_chk.pack(side='left', padx=(12, 0))

        exact_tools = ttk.Frame(parent, padding=(8, 4))
        exact_tools.pack(fill='x')
        self._duped_exact_tools = exact_tools
        self.duped_links_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            exact_tools,
            text='Create symlinks in other galleries',
            variable=self.duped_links_var,
        ).pack(side='left')
        ttk.Button(
            exact_tools,
            text='Move to home \u2192 selected',
            command=lambda: self.duped_apply_home(scope='selected'),
        ).pack(side='left', padx=(8, 2))
        ttk.Button(
            exact_tools,
            text='Move to home \u2192 all listed',
            command=lambda: self.duped_apply_home(scope='all'),
        ).pack(side='left', padx=2)
        ttk.Button(
            exact_tools,
            text='Strip peers \u2192 selected',
            command=lambda: self.duped_strip_peers(scope='selected'),
        ).pack(side='left', padx=(8, 2))
        ttk.Button(
            exact_tools,
            text='Strip peers \u2192 all listed',
            command=lambda: self.duped_strip_peers(scope='all'),
        ).pack(side='left', padx=2)

        near_tools = ttk.Frame(parent, padding=(8, 4))
        self._duped_near_tools = near_tools
        ttk.Label(near_tools, text='Max Hamming:').pack(side='left')
        self.duped_hamming_var = tk.IntVar(value=DEFAULT_MAX_HAMMING)
        ttk.Spinbox(
            near_tools,
            from_=0,
            to=16,
            width=4,
            textvariable=self.duped_hamming_var,
        ).pack(side='left', padx=4)
        ttk.Button(
            near_tools, text='Fill dHash', command=self.duped_fill_dhash
        ).pack(side='left', padx=(8, 2))
        ttk.Button(
            near_tools, text='Rebuild near pairs', command=self.duped_rebuild_near
        ).pack(side='left', padx=2)
        ttk.Button(
            near_tools,
            text='Mark false positive',
            command=self.duped_mark_false_positive,
        ).pack(side='left', padx=(12, 2))

        body = ttk.Panedwindow(parent, orient='vertical')
        body.pack(fill='both', expand=True, padx=8, pady=8)
        self._duped_body_paned = body

        lists = ttk.Panedwindow(body, orient='horizontal')
        body.add(lists, weight=3)

        left = ttk.Frame(lists)
        right = ttk.Frame(lists)
        lists.add(left, weight=1)
        lists.add(right, weight=2)

        gcols = ('gallery', 'shared', 'peers', 'folder')
        self.duped_gallery_tree = ttk.Treeview(
            left,
            columns=gcols,
            show='headings',
            selectmode='browse',
            height=10,
        )
        self._duped_bind_sortable_headings(
            self.duped_gallery_tree,
            {
                'gallery': 'Gallery',
                'shared': 'Shared',
                'peers': 'Peers',
                'folder': 'Folder',
            },
            which='gallery',
        )
        self.duped_gallery_tree.column('gallery', width=90, stretch=False)
        self.duped_gallery_tree.column('shared', width=55, stretch=False, anchor='center')
        self.duped_gallery_tree.column('peers', width=45, stretch=False, anchor='center')
        self.duped_gallery_tree.column('folder', width=220, stretch=True)
        gscroll = ttk.Scrollbar(
            left, orient='vertical', command=self.duped_gallery_tree.yview
        )
        self.duped_gallery_tree.configure(yscrollcommand=gscroll.set)
        self.duped_gallery_tree.pack(side='left', fill='both', expand=True)
        gscroll.pack(side='right', fill='y')
        self.duped_gallery_tree.bind('<<TreeviewSelect>>', self._on_duped_gallery_select)

        fcols = ('name', 'this_path', 'peer', 'peer_path', 'home')
        legend = ttk.Frame(right)
        legend.pack(fill='x', pady=(0, 4))
        tk.Label(
            legend,
            text='  This gallery  ',
            fg='white',
            bg=DUPED_COLOR_LEFT,
            font=('Segoe UI', 8, 'bold'),
        ).pack(side='left')
        ttk.Label(legend, text='  vs  ').pack(side='left')
        tk.Label(
            legend,
            text='  Peer  ',
            fg='white',
            bg=DUPED_COLOR_RIGHT,
            font=('Segoe UI', 8, 'bold'),
        ).pack(side='left')
        ttk.Label(
            legend,
            text='  (hover \u00b13 \u00b7 double-click \u2192 large compare)',
            foreground='#666666',
        ).pack(side='left', padx=8)

        tree_wrap = ttk.Frame(right)
        tree_wrap.pack(fill='both', expand=True)
        self.duped_file_tree = ttk.Treeview(
            tree_wrap,
            columns=fcols,
            show='headings',
            selectmode='extended',
            height=10,
        )
        self._duped_bind_sortable_headings(
            self.duped_file_tree,
            {
                'name': 'This name',
                'this_path': 'This path',
                'peer': 'Peer name',
                'peer_path': 'Peer path',
                'home': 'Home',
            },
            which='file',
        )
        self.duped_file_tree.column('name', width=140, stretch=False)
        self.duped_file_tree.column('this_path', width=220, stretch=True)
        self.duped_file_tree.column('peer', width=140, stretch=False)
        self.duped_file_tree.column('peer_path', width=220, stretch=True)
        self.duped_file_tree.column('home', width=70, stretch=False)
        fscroll = ttk.Scrollbar(
            tree_wrap, orient='vertical', command=self.duped_file_tree.yview
        )
        self.duped_file_tree.configure(yscrollcommand=fscroll.set)
        self.duped_file_tree.pack(side='left', fill='both', expand=True)
        fscroll.pack(side='right', fill='y')
        self.duped_file_tree.bind('<Motion>', self._on_duped_file_motion)
        self.duped_file_tree.bind('<Leave>', self._on_duped_file_leave)
        self.duped_file_tree.bind(
            '<ButtonPress-1>', lambda _e: self._duped_hide_preview()
        )
        self.duped_file_tree.bind('<MouseWheel>', lambda _e: self._duped_hide_preview())
        self.duped_file_tree.bind('<Button-3>', self._on_duped_file_context)
        self.duped_file_tree.bind('<<TreeviewSelect>>', self._on_duped_file_select)
        self.duped_file_tree.bind('<Double-1>', self._on_duped_file_double)
        self._duped_file_menu = tk.Menu(self, tearoff=0)
        self.duped_gallery_tree.bind('<ButtonRelease-1>', self._on_duped_gallery_click)

        compare = ttk.LabelFrame(
            body,
            text='Match board (index · double-click / Compare \u2192 large window)',
            padding=4,
        )
        body.add(compare, weight=3)
        self._duped_compare_frame = compare

        board_wrap = ttk.Frame(compare)
        board_wrap.pack(fill='both', expand=True)
        canvas = tk.Canvas(board_wrap, highlightthickness=0, bg='#1e1e1e')
        vscroll = ttk.Scrollbar(board_wrap, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = ttk.Frame(canvas)
        self._duped_board_window = canvas.create_window((0, 0), window=inner, anchor='nw')
        self._duped_board_canvas = canvas
        self._duped_compare_inner = inner

        def _on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def _on_canvas_configure(event):
            canvas.itemconfigure(self._duped_board_window, width=event.width)

        def _on_board_scroll(event):
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), 'units')
            self._duped_board_maybe_load_more()

        inner.bind('<Configure>', _on_inner_configure)
        canvas.bind('<Configure>', _on_canvas_configure)
        canvas.bind('<MouseWheel>', _on_board_scroll)
        inner.bind('<MouseWheel>', _on_board_scroll)

        ttk.Label(
            inner,
            text='Select a gallery — match cards appear here (no click-per-row needed).',
            foreground='#888888',
        ).pack(anchor='w', padx=4, pady=8)

        self.duped_status = tk.StringVar(
            value='Refresh to list galleries with shared fingerprints.'
        )
        ttk.Label(parent, textvariable=self.duped_status, padding=(8, 4)).pack(fill='x')
        self._duped_apply_mode_ui()

    def _request_reload(self):
        shell = self.winfo_toplevel()
        if hasattr(shell, 'reload_app'):
            shell.reload_app()

    def prepare_for_reload(self):
        """Stop workers before the shell destroys this frame (WishAssistance-style)."""
        self._lifecycle_alive = False
        self._ui_drain_alive = False
        self._duped_file_pop_gen += 1
        self._stop.set()
        self._import_stop.set()
        self._duped_stop.set()
        self._duped_hide_preview()
        self._duped_close_compare_win()
        with self._ui_pending_lock:
            self._ui_pending.clear()
        with self._duped_thumb_ready_lock:
            self._duped_thumb_ready.clear()
        hw = self._hash_worker
        self._hash_worker = None
        if hw:
            try:
                hw.stop(join_timeout=1.0)
            except Exception:
                pass
        dw = self._dhash_worker
        self._dhash_worker = None
        if dw:
            try:
                dw.stop(join_timeout=1.0)
            except Exception:
                pass
        try:
            self._duped_thumb_gen += 1
            self._duped_thumb_queue.put_nowait(None)
        except Exception:
            pass
        if self._current_url and self.store:
            try:
                self.store.mark_stopped(self._current_url)
            except Exception:
                pass
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=1.5)
        self._worker = None
        self._current_url = None
        log.info('prepare_for_reload: workers stopped')

    def _start_ui_drain(self):
        """Periodic UI drain — only the main thread may call Tk/after."""
        self._ui_drain_alive = True
        try:
            self.after(UI_DRAIN_MS, self._drain_ui_pending)
        except tk.TclError:
            self._ui_drain_alive = False

    def _ui_schedule(self, fn, *args):
        """Enqueue UI work from any thread (no Tk calls here)."""
        if not self._lifecycle_alive or not self._ui_drain_alive:
            return
        with self._ui_pending_lock:
            self._ui_pending.append((fn, args))

    def _drain_ui_pending(self):
        if not self._ui_drain_alive or not self._lifecycle_alive:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        self._drain_duped_thumb_ready()

        batch: list[tuple[object, tuple]] = []
        with self._ui_pending_lock:
            while self._ui_pending and len(batch) < UI_DRAIN_BATCH:
                batch.append(self._ui_pending.popleft())
        for fn, args in batch:
            if not self._lifecycle_alive:
                break
            try:
                fn(*args)
            except tk.TclError:
                pass
            except Exception:
                log.exception('UI drain callback failed')

        if not self._ui_drain_alive or not self._lifecycle_alive:
            return
        try:
            self.after(UI_DRAIN_MS, self._drain_ui_pending)
        except tk.TclError:
            self._ui_drain_alive = False

    def _drain_duped_thumb_ready(self):
        """Apply a bounded number of decoded thumbs per tick (main thread only)."""
        jobs = []
        with self._duped_thumb_ready_lock:
            while self._duped_thumb_ready and len(jobs) < DUPED_THUMB_APPLY_PER_TICK:
                jobs.append(self._duped_thumb_ready.popleft())
        for gen, lbl, image, key in jobs:
            if gen != self._duped_thumb_gen or not self._lifecycle_alive:
                continue
            try:
                from PIL import ImageTk

                photo = ImageTk.PhotoImage(image)
            except Exception:
                continue
            self._duped_thumb_cache[key] = photo
            self._duped_compare_photos.append(photo)
            if self._duped_compare_win is not None:
                self._duped_compare_win_photos.append(photo)
            try:
                if lbl.winfo_exists():
                    lbl.configure(image=photo, text='')
            except Exception:
                pass

    # --- Duped tab ---

    def _duped_bind_sortable_headings(
        self,
        tree: ttk.Treeview,
        labels: dict[str, str],
        *,
        which: str,
    ) -> None:
        tree._duped_heading_labels = labels  # type: ignore[attr-defined]
        for col, text in labels.items():
            tree.heading(
                col,
                text=text,
                command=lambda c=col, w=which: self._duped_sort_by(w, c),
            )

    def _duped_sort_key(self, value: str, *, numeric: bool):
        s = (value or '').strip()
        if numeric:
            try:
                return (0, int(s))
            except ValueError:
                return (1, s.casefold())
        return s.casefold()

    def _duped_sort_by(self, which: str, col: str):
        if which == 'gallery':
            tree = self.duped_gallery_tree
            prev_col, prev_rev = self._duped_gallery_sort
            numeric_cols = {'shared', 'peers'}
        else:
            tree = self.duped_file_tree
            prev_col, prev_rev = self._duped_file_sort
            numeric_cols = {'home'}  # Home gallery key or Near Hamming dist

        reverse = not prev_rev if col == prev_col else False
        if which == 'gallery':
            self._duped_gallery_sort = (col, reverse)
        else:
            self._duped_file_sort = (col, reverse)

        numeric = col in numeric_cols
        rows = [
            (self._duped_sort_key(tree.set(iid, col), numeric=numeric), iid)
            for iid in tree.get_children('')
        ]
        rows.sort(key=lambda t: t[0], reverse=reverse)
        for index, (_key, iid) in enumerate(rows):
            tree.move(iid, '', index)

        labels = getattr(tree, '_duped_heading_labels', {})
        for c, base in labels.items():
            mark = ''
            if c == col:
                mark = ' ▼' if reverse else ' ▲'
            tree.heading(c, text=base + mark)

    def _duped_cancel_preview_timer(self):
        aid = self._duped_preview_after
        self._duped_preview_after = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                pass

    def _duped_hide_preview(self):
        self._duped_cancel_preview_timer()
        self._duped_preview_iid = None
        self._duped_preview_path = None
        self._duped_preview_ctx = None
        win = self._duped_preview
        self._duped_preview = None
        self._duped_preview_photos = []
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _on_duped_file_leave(self, _event=None):
        win = self._duped_preview
        if win is not None:
            try:
                px, py = win.winfo_pointerxy()
                wx = win.winfo_rootx()
                wy = win.winfo_rooty()
                if (
                    wx <= px <= wx + win.winfo_width()
                    and wy <= py <= wy + win.winfo_height()
                ):
                    return
            except Exception:
                pass
        self._duped_hide_preview()

    def _on_duped_file_motion(self, event):
        if not self._lifecycle_alive:
            return
        tree = self.duped_file_tree
        iid = tree.identify_row(event.y)
        if not iid:
            self._duped_hide_preview()
            return
        if iid == self._duped_preview_iid and self._duped_preview is not None:
            self._duped_place_preview(event.x_root, event.y_root)
            return
        if self._duped_preview is not None:
            win = self._duped_preview
            self._duped_preview = None
            self._duped_preview_photos = []
            self._duped_preview_path = None
            self._duped_preview_ctx = None
            try:
                win.destroy()
            except Exception:
                pass
        self._duped_cancel_preview_timer()
        self._duped_preview_iid = iid
        self._duped_preview_after = self.after(
            140,
            lambda: self._duped_show_preview(iid, event.x_root, event.y_root),
        )

    def _duped_place_preview(self, x_root: int, y_root: int):
        win = self._duped_preview
        if win is None:
            return
        try:
            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            x = min(x_root + 16, sw - w - 8)
            y = min(y_root + 16, sh - h - 8)
            x = max(8, x)
            y = max(8, y)
            win.geometry(f'+{x}+{y}')
        except Exception:
            pass

    def _duped_folder_path_for_key(self, gallery_key: str) -> Path | None:
        """Configured out_dir for a gallery key (may not exist on disk yet)."""
        row = self._duped_rows.get(gallery_key) or {}
        out = row.get('out_dir')
        if not out and self.store:
            try:
                meta = self.store.resolve_gallery_meta(gallery_key)
            except Exception:
                meta = None
            out = (meta or {}).get('out_dir')
        return Path(out) if out else None

    def _duped_out_dir_for_key(self, gallery_key: str) -> Path | None:
        p = self._duped_folder_path_for_key(gallery_key)
        return p if p is not None and p.is_dir() else None

    def _duped_display_path(self, gallery_key: str, name: str) -> str:
        """Absolute path for UI; append (missing) when the file is gone."""
        folder = self._duped_folder_path_for_key(gallery_key)
        if folder is None:
            return '(no folder)'
        if name:
            path = folder / name
            if path.exists() or path.is_symlink():
                return str(path)
            return f'{path} (missing)'
        if folder.is_dir():
            return str(folder)
        return f'{folder} (missing)'

    def _duped_slot_path(
        self, folder: Path | None, name: str, sample_path: str | None
    ) -> Path | None:
        """Resolve on-disk bytes for a gallery slot without borrowing another gallery's file.

        After move-without-links, peer slots are often missing — return None rather
        than matching the home copy (that skews the neighbor index).
        """
        if folder is not None and name:
            local = folder / name
            if local.exists() or local.is_symlink():
                return local
        if sample_path and folder is not None:
            try:
                sp = Path(sample_path)
                # Only accept sample_path if it still lives under this gallery folder.
                if same_path(sp.parent, folder) and (sp.exists() or sp.is_symlink()):
                    return sp
            except OSError:
                pass
        return None

    def _duped_sequence(self, gallery_key: str) -> list[dict]:
        """Alias-ordered slots for a gallery: ``{name, path|None}``.

        Order comes from DB aliases (gallery identity), not current disk listing —
        disk alone is wrong after move-without-links.
        """
        if gallery_key in self._duped_seq_cache:
            return self._duped_seq_cache[gallery_key]
        folder = self._duped_out_dir_for_key(gallery_key)
        slots: list[dict] = []
        aliases: list[dict] = []
        if self.store:
            try:
                aliases = self.store.list_gallery_ordered_names(gallery_key)
            except Exception:
                aliases = []
        if aliases:
            # Stable natural order (zero-padded names sort correctly via nat_key).
            aliases = sorted(
                aliases,
                key=lambda a: nat_key(a.get('name') or a.get('bare_name') or ''),
            )
            seen: set[str] = set()
            for a in aliases:
                name = (a.get('name') or a.get('bare_name') or '').strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                slots.append(
                    {
                        'name': name,
                        'path': self._duped_slot_path(
                            folder, name, a.get('sample_path')
                        ),
                        'sha1': a.get('sha1'),
                    }
                )
        elif folder is not None:
            # No aliases yet — disk order only.
            for pth in sorted(list_images(folder), key=lambda x: nat_key(x.name)):
                slots.append({'name': pth.name, 'path': pth, 'sha1': None})
        self._duped_seq_cache[gallery_key] = slots
        return slots

    def _duped_alias_name(self, item: dict, gallery_key: str) -> str:
        for a in item.get('aliases') or []:
            if a.get('gallery_key') == gallery_key:
                return (a.get('name') or a.get('bare_name') or '').strip()
        return ''

    def _duped_index_in_seq(
        self, seq: list[dict], item: dict, gallery_key: str
    ) -> int | None:
        """Index of this SHA's alias name in the gallery sequence (exact name only)."""
        if not seq:
            return None
        name = self._duped_alias_name(item, gallery_key)
        if not name:
            return None
        for i, slot in enumerate(seq):
            if slot.get('name') == name:
                return i
        # Try bare thumb vs ordered name either direction.
        bare = name.split('_', 1)[-1] if '_' in name and name.split('_', 1)[0].isdigit() else name
        for i, slot in enumerate(seq):
            sn = slot.get('name') or ''
            if sn == bare:
                return i
            if '_' in sn and sn.split('_', 1)[0].isdigit() and sn.split('_', 1)[-1] == bare:
                return i
            if '_' in name and name.split('_', 1)[0].isdigit() and name.split('_', 1)[-1] == sn:
                return i
        return None

    def _duped_peer_key(self, item: dict, focus_key: str) -> str | None:
        peers = sorted({
            a.get('gallery_key') or ''
            for a in item.get('aliases') or []
            if (a.get('gallery_key') or '') and a.get('gallery_key') != focus_key
        })
        # Prefer a peer that is not already the fingerprint home when focus is home.
        home = (item.get('home_gallery_key') or '').strip()
        if home and home != focus_key and home in peers:
            return home
        return peers[0] if peers else None

    def _reveal_in_explorer(
        self, select_path: Path | None, folder: Path | None
    ) -> None:
        """Open Explorer with ``select_path`` highlighted, else open ``folder``."""
        try:
            if select_path is not None:
                try:
                    if select_path.exists() or select_path.is_symlink():
                        # absolute() — do not follow symlinks out of this gallery.
                        abs_path = str(select_path.absolute())
                        self._explorer_select(abs_path)
                        return
                except OSError:
                    pass
            target = folder
            if target is None and select_path is not None:
                target = select_path.parent
            if target is not None and target.is_dir():
                # Prefer selecting any image so the window still focuses a file.
                try:
                    images = sorted(
                        list_images(target), key=lambda p: nat_key(p.name)
                    )
                except Exception:
                    images = []
                if images:
                    self._explorer_select(str(images[0].absolute()))
                    return
                self._explorer_open_folder(str(target.absolute()))
                return
        except Exception as e:
            messagebox.showerror('Explorer', f'Could not open:\n{e}')
            return
        messagebox.showinfo('Explorer', 'Folder not found on disk.')

    @staticmethod
    def _explorer_select(abs_path: str) -> None:
        """Highlight a file in Explorer (reliable vs explorer /select)."""
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32

        ILCreateFromPathW = shell32.ILCreateFromPathW
        ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
        ILCreateFromPathW.restype = ctypes.c_void_p

        SHOpenFolderAndSelectItems = shell32.SHOpenFolderAndSelectItems
        SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        SHOpenFolderAndSelectItems.restype = ctypes.HRESULT

        ILFree = shell32.ILFree
        ILFree.argtypes = [ctypes.c_void_p]

        hr_init = int(ole32.CoInitialize(None) or 0)
        # S_OK (0) → we own uninit; S_FALSE (1) → already inited on this thread.
        need_uninit = hr_init == 0
        try:
            pidl = ILCreateFromPathW(abs_path)
            if not pidl:
                raise OSError(f'ILCreateFromPathW failed for {abs_path}')
            try:
                hr = SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                if hr:
                    raise OSError(
                        f'SHOpenFolderAndSelectItems failed ({hr:#x}) '
                        f'for {abs_path}'
                    )
            finally:
                ILFree(pidl)
        finally:
            if need_uninit:
                ole32.CoUninitialize()

    @staticmethod
    def _explorer_open_folder(abs_dir: str) -> None:
        import ctypes

        rc = ctypes.windll.shell32.ShellExecuteW(
            None,
            'open',
            'explorer.exe',
            f'"{abs_dir}"',
            None,
            1,
        )
        if rc <= 32:
            raise OSError(f'ShellExecute failed ({rc}) for {abs_dir}')

    @staticmethod
    def _duped_path_present(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.exists() or path.is_symlink()
        except OSError:
            return False

    def _duped_reveal_for_gallery(self, gallery_key: str, item: dict) -> None:
        """Select focus file in Explorer, or nearest on-disk neighbor, else folder."""
        folder = self._duped_out_dir_for_key(gallery_key)
        folder_any = self._duped_folder_path_for_key(gallery_key)
        select = self._duped_nearest_reveal_path(gallery_key, item)
        self._reveal_in_explorer(select, folder or folder_any)

    def _duped_nearest_reveal_path(
        self, gallery_key: str, item: dict
    ) -> Path | None:
        """Focus file if present; else closest on-disk neighbor in gallery order."""
        seq = self._duped_sequence(gallery_key)
        idx = self._duped_index_in_seq(seq, item, gallery_key)
        if idx is not None and seq:
            path = seq[idx].get('path')
            if self._duped_path_present(path):
                return path
            for dist in range(1, len(seq)):
                for j in (idx - dist, idx + dist):
                    if 0 <= j < len(seq):
                        cand = seq[j].get('path')
                        if self._duped_path_present(cand):
                            return cand

        folder = self._duped_out_dir_for_key(gallery_key)
        if folder is None:
            return None
        try:
            files = sorted(list_images(folder), key=lambda p: nat_key(p.name))
        except Exception:
            files = []
        if not files:
            return None
        name = self._duped_alias_name(item, gallery_key)
        if not name:
            return files[0]
        target = nat_key(name)
        # Insertion point among existing files → pick nearer side.
        insert_at = 0
        while insert_at < len(files) and nat_key(files[insert_at].name) < target:
            insert_at += 1
        if insert_at >= len(files):
            return files[-1]
        if insert_at == 0:
            return files[0]
        # Between two existing files — pick the previous (closer/earlier neighbor).
        return files[insert_at - 1]

    def _on_duped_file_context(self, event):
        self._duped_hide_preview()
        tree = self.duped_file_tree
        iid = tree.identify_row(event.y)
        if not iid:
            return
        if iid not in tree.selection():
            tree.selection_set(iid)
            tree.focus(iid)
        self._duped_popup_compare_menu(
            event.x_root, event.y_root, iid=iid
        )

    def _on_duped_preview_context(self, event):
        ctx = self._duped_preview_ctx or {}
        iid = ctx.get('iid')
        if not iid:
            return
        self._duped_popup_compare_menu(
            event.x_root, event.y_root, iid=iid
        )
        return 'break'

    def _duped_popup_compare_menu(
        self, x_root: int, y_root: int, *, iid: str
    ) -> None:
        item = self._duped_files.get(iid)
        focus_key = self._duped_focus_key
        if not item or not focus_key:
            return
        peer_key = self._duped_peer_key(item, focus_key)
        this_name = self._duped_alias_name(item, focus_key) or focus_key
        menu = self._duped_file_menu
        menu.delete(0, 'end')
        menu.add_command(
            label=f'Open this gallery in Explorer  ({focus_key})',
            command=lambda: self._duped_reveal_for_gallery(focus_key, item),
        )
        if peer_key:
            peer_name = self._duped_alias_name(item, peer_key) or peer_key
            menu.add_command(
                label=f'Open peer gallery in Explorer  ({peer_key})',
                command=lambda: self._duped_reveal_for_gallery(peer_key, item),
            )
            menu.add_separator()
            menu.add_command(
                label=f'This: {this_name}',
                state='disabled',
            )
            menu.add_command(
                label=f'Peer: {peer_name}',
                state='disabled',
            )
        else:
            menu.add_command(label='Open peer gallery…', state='disabled')
        try:
            menu.tk_popup(x_root, y_root)
        finally:
            menu.grab_release()

    def _duped_load_thumb(
        self,
        path: Path | None,
        size: int = DUPED_THUMB_SIZE,
        *,
        box: tuple[int, int] | None = None,
        fast: bool = False,
    ):
        """Return PhotoImage or None (sync — prefer ``_duped_queue_thumb`` for board)."""
        img = self._duped_decode_thumb_image(path, size=size, box=box, fast=fast)
        if img is None:
            return None
        try:
            from PIL import ImageTk

            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    @staticmethod
    def _duped_decode_thumb_image(
        path: Path | None,
        *,
        size: int = DUPED_THUMB_SIZE,
        box: tuple[int, int] | None = None,
        fast: bool = False,
    ):
        """Decode to a PIL RGB image (thread-safe)."""
        if path is None:
            return None
        try:
            from PIL import Image
        except ImportError:
            return None
        try:
            with Image.open(path) as im:
                if box is not None:
                    bw, bh = int(box[0]), int(box[1])
                    if fast:
                        try:
                            im.draft('RGB', (bw, bh))
                        except Exception:
                            pass
                    if im.mode != 'RGB':
                        im = im.convert('RGB')
                    else:
                        im = im.copy()
                    resample = (
                        Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS
                    )
                    im.thumbnail((bw, bh), resample)
                    canvas = Image.new('RGB', (bw, bh), (32, 32, 32))
                    canvas.paste(
                        im, ((bw - im.width) // 2, (bh - im.height) // 2)
                    )
                    return canvas
                if fast:
                    try:
                        im.draft('RGB', (size, size))
                    except Exception:
                        pass
                if im.mode != 'RGB':
                    im = im.convert('RGB')
                else:
                    im = im.copy()
                resample = (
                    Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS
                )
                im.thumbnail((size, size), resample)
                canvas = Image.new('RGB', (size, size), (40, 40, 40))
                canvas.paste(
                    im, ((size - im.width) // 2, (size - im.height) // 2)
                )
                return canvas
        except Exception:
            return None

    def _duped_ensure_thumb_worker(self):
        t = self._duped_thumb_thread
        if t is not None and t.is_alive():
            return

        def run():
            while True:
                job = self._duped_thumb_queue.get()
                if job is None:
                    break
                gen, label, path, box, cache_key, fast = job
                if gen != self._duped_thumb_gen or not self._lifecycle_alive:
                    continue
                img = self._duped_decode_thumb_image(
                    path, box=box, fast=fast
                )
                if img is None:
                    continue
                if gen != self._duped_thumb_gen or not self._lifecycle_alive:
                    continue
                with self._duped_thumb_ready_lock:
                    while len(self._duped_thumb_ready) >= DUPED_THUMB_READY_MAX:
                        self._duped_thumb_ready.popleft()
                    self._duped_thumb_ready.append((gen, label, img, cache_key))

        self._duped_thumb_thread = threading.Thread(
            target=run, name='duped-thumbs', daemon=True
        )
        self._duped_thumb_thread.start()

    def _duped_queue_thumb(
        self,
        label: tk.Label,
        path: Path | None,
        box: tuple[int, int],
        *,
        fast: bool = True,
    ) -> None:
        """Show cached thumb or queue async decode (keeps UI responsive)."""
        if path is None:
            return
        cache_key = f'{path}|{box[0]}x{box[1]}|{"f" if fast else "q"}'
        cached = self._duped_thumb_cache.get(cache_key)
        if cached is not None:
            try:
                label.configure(image=cached, text='')
                if self._duped_compare_win is not None:
                    self._duped_compare_win_photos.append(cached)
            except Exception:
                pass
            return
        self._duped_ensure_thumb_worker()
        self._duped_thumb_queue.put(
            (self._duped_thumb_gen, label, path, box, cache_key, fast)
        )

    def _duped_build_strip(
        self,
        parent,
        *,
        gallery_key: str,
        seq: list[dict],
        center_idx: int | None,
        border: str,
        title: str,
    ) -> None:
        col = tk.Frame(parent, bg='#1a1a1a', highlightbackground=border, highlightthickness=3)
        col.pack(side='left', padx=4, pady=2, fill='y')
        tk.Label(
            col,
            text=f'{title}  {gallery_key}',
            fg=border,
            bg='#1a1a1a',
            font=('Segoe UI', 9, 'bold'),
        ).pack(pady=(4, 0))
        folder = self._duped_folder_path_for_key(gallery_key)
        folder_txt = str(folder) if folder is not None else '(no folder)'
        if len(folder_txt) > 52:
            folder_txt = folder_txt[:22] + '…' + folder_txt[-26:]
        tk.Label(
            col,
            text=folder_txt,
            fg='#9ae6b4' if folder is not None and folder.is_dir() else '#fc8181',
            bg='#1a1a1a',
            font=('Segoe UI', 7),
        ).pack(pady=(0, 2))
        radius = DUPED_NEIGHBOR_RADIUS
        if center_idx is None:
            tk.Label(
                col, text='(no sequence match)', fg='#aaa', bg='#1a1a1a'
            ).pack(padx=8, pady=8)
            return
        start = max(0, center_idx - radius)
        end = min(len(seq), center_idx + radius + 1)
        for i in range(start, end):
            slot = seq[i]
            name = slot.get('name') or '?'
            path = slot.get('path')
            is_focus = i == center_idx
            cell_border = DUPED_COLOR_FOCUS if is_focus else border
            thick = 4 if is_focus else 2
            cell = tk.Frame(
                col,
                bg='#111',
                highlightbackground=cell_border,
                highlightthickness=thick,
            )
            cell.pack(padx=6, pady=2)
            photo = self._duped_load_thumb(path, fast=True)
            if photo is not None:
                self._duped_preview_photos.append(photo)
                tk.Label(cell, image=photo, bg='#111').pack()
            else:
                tk.Label(
                    cell,
                    text='missing' if path is None else '?',
                    width=10,
                    height=4,
                    fg='#f6ad55' if path is None else '#888',
                    bg='#111',
                ).pack()
            tk.Label(
                cell,
                text=name[:28],
                fg='#ddd' if is_focus else '#999',
                bg='#111',
                font=('Segoe UI', 7, 'bold' if is_focus else 'normal'),
            ).pack()
            if is_focus:
                focus_path = (
                    str(path)
                    if path is not None
                    else self._duped_display_path(gallery_key, name)
                )
                if len(focus_path) > 42:
                    focus_path = focus_path[:18] + '…' + focus_path[-20:]
                tk.Label(
                    cell,
                    text=focus_path,
                    fg='#9ae6b4' if path is not None else '#fc8181',
                    bg='#111',
                    font=('Segoe UI', 6),
                ).pack()

    def _duped_show_preview(self, iid: str, x_root: int, y_root: int):
        self._duped_preview_after = None
        if not self._lifecycle_alive or iid != self._duped_preview_iid:
            return
        item = self._duped_files.get(iid)
        focus_key = self._duped_focus_key
        if not item or not focus_key:
            self._duped_hide_preview()
            return

        peer_key = self._duped_peer_key(item, focus_key)
        left_seq = self._duped_sequence(focus_key)
        left_idx = self._duped_index_in_seq(left_seq, item, focus_key)
        right_seq: list[dict] = []
        right_idx: int | None = None
        if peer_key:
            right_seq = self._duped_sequence(peer_key)
            right_idx = self._duped_index_in_seq(right_seq, item, peer_key)

        cache_key = f'{iid}:{focus_key}:{peer_key}:{left_idx}:{right_idx}'
        if (
            self._duped_preview is not None
            and self._duped_preview_path == cache_key
        ):
            self._duped_place_preview(x_root, y_root)
            return

        win_old = self._duped_preview
        self._duped_preview = None
        self._duped_preview_photos = []
        if win_old is not None:
            try:
                win_old.destroy()
            except Exception:
                pass

        self._duped_preview_iid = iid
        self._duped_preview_path = cache_key
        self._duped_preview_ctx = {
            'iid': iid,
            'item': item,
            'focus_key': focus_key,
            'peer_key': peer_key,
        }
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        try:
            win.attributes('-topmost', True)
        except Exception:
            pass
        win.configure(background='#1a1a1a')
        win.bind('<Button-3>', self._on_duped_preview_context)
        body = tk.Frame(win, bg='#1a1a1a', padx=6, pady=6)
        body.pack()
        tk.Label(
            body,
            text='slice · 3 above / focus / 3 below · right-click → Explorer',
            fg='#ccc',
            bg='#1a1a1a',
            font=('Segoe UI', 8),
        ).pack(anchor='w')
        strips = tk.Frame(body, bg='#1a1a1a')
        strips.pack()
        self._duped_build_strip(
            strips,
            gallery_key=focus_key,
            seq=left_seq,
            center_idx=left_idx,
            border=DUPED_COLOR_LEFT,
            title='This gallery',
        )
        if peer_key:
            self._duped_build_strip(
                strips,
                gallery_key=peer_key,
                seq=right_seq,
                center_idx=right_idx,
                border=DUPED_COLOR_RIGHT,
                title='Peer',
            )
        else:
            tk.Label(
                strips, text='(no peer gallery)', fg='#888', bg='#1a1a1a'
            ).pack(side='left', padx=12)

        def _bind_rclick(w):
            w.bind('<Button-3>', self._on_duped_preview_context)
            for child in w.winfo_children():
                _bind_rclick(child)

        _bind_rclick(win)
        win.bind('<Leave>', self._on_duped_preview_leave)

        self._duped_preview = win
        self._duped_place_preview(x_root, y_root)

    def _on_duped_preview_leave(self, event):
        win = self._duped_preview
        if win is None or event.widget is not win:
            return
        try:
            px, py = win.winfo_pointerxy()
            wx = win.winfo_rootx()
            wy = win.winfo_rooty()
            if (
                wx <= px <= wx + win.winfo_width()
                and wy <= py <= wy + win.winfo_height()
            ):
                return
        except Exception:
            pass
        self._duped_hide_preview()

    def _duped_is_near(self) -> bool:
        return (getattr(self, 'duped_mode_var', None) and
                self.duped_mode_var.get() == 'near') or self._duped_mode == 'near'

    def _on_duped_mode_change(self):
        self._duped_mode = self.duped_mode_var.get()
        self._duped_apply_mode_ui()
        self.duped_refresh()

    def _duped_apply_mode_ui(self):
        near = self._duped_is_near()
        if near:
            self._duped_exact_tools.pack_forget()
            self._duped_near_tools.pack(fill='x')
            try:
                self.duped_undecided_chk.state(['disabled'])
            except Exception:
                pass
            labels = {
                'name': 'This name',
                'this_path': 'This path',
                'peer': 'Peer name',
                'peer_path': 'Peer path',
                'home': 'Dist',
            }
        else:
            self._duped_near_tools.pack_forget()
            self._duped_exact_tools.pack(fill='x')
            try:
                self.duped_undecided_chk.state(['!disabled'])
            except Exception:
                pass
            labels = {
                'name': 'This name',
                'this_path': 'This path',
                'peer': 'Peer name',
                'peer_path': 'Peer path',
                'home': 'Home',
            }
        self._duped_bind_sortable_headings(
            self.duped_file_tree, labels, which='file'
        )

    def _start_dhash_worker(self):
        if not self.store or self._dhash_worker is not None:
            return

        def on_progress(msg: str):
            if not self._lifecycle_alive:
                return

            def apply():
                if not self._lifecycle_alive:
                    return
                if self._duped_is_near():
                    self.duped_status.set(msg)

            self._ui_schedule(apply)

        try:
            ham = int(self.duped_hamming_var.get()) if hasattr(self, 'duped_hamming_var') else DEFAULT_MAX_HAMMING
        except Exception:
            ham = DEFAULT_MAX_HAMMING
        worker = DhashFillWorker(
            self.store,
            max_hamming=ham,
            on_progress=on_progress,
            enabled=True,
        )
        self._dhash_worker = worker
        worker.start()

    def duped_fill_dhash(self):
        if not self.store:
            return
        w = self._dhash_worker
        if w is None:
            self._start_dhash_worker()
            w = self._dhash_worker
        if w is None:
            return
        try:
            w.set_max_hamming(int(self.duped_hamming_var.get()))
        except Exception:
            pass
        w.set_enabled(True)
        w.wake()
        try:
            stats = self.store.dhash_fill_stats()
        except Exception as e:
            messagebox.showerror('Duped', f'dHash stats failed:\n{e}')
            return
        skip = stats.get('failed') or 0
        skip_s = f', {skip} unreadable' if skip else ''
        self.duped_status.set(
            f"Filling dHash — {stats['filled']}/{stats['total']} "
            f"({stats['missing']} left{skip_s})"
        )

    def duped_rebuild_near(self):
        if not self.store:
            messagebox.showwarning('Duped', 'Database not ready.')
            return
        if self._duped_busy:
            messagebox.showinfo('Duped', 'Busy — wait for the current job.')
            return
        try:
            ham = int(self.duped_hamming_var.get())
        except Exception:
            ham = DEFAULT_MAX_HAMMING
        if not messagebox.askyesno(
            'Duped',
            f'Rebuild near pairs from all filled dHashes?\n'
            f'Max Hamming = {ham}\n\n'
            f'This runs in the background (BK-tree over all fingerprints).',
        ):
            return
        self._duped_busy = True
        self.duped_status.set('Rebuilding near pairs…')

        def work():
            err = None
            stats = {}
            try:
                w = self._dhash_worker
                if w is not None:
                    w.set_max_hamming(ham)
                    stats = w.full_rebuild_pairs()
                else:
                    stats = self.store.rebuild_dhash_near_pairs(max_hamming=ham)
            except Exception as e:
                err = e

            def done():
                self._duped_busy = False
                if not self._lifecycle_alive:
                    return
                if err:
                    messagebox.showerror('Duped', f'Rebuild failed:\n{err}')
                    self.duped_status.set('Near rebuild failed')
                    return
                self.duped_status.set(
                    f"Near pairs rebuilt — {stats.get('pairs', 0)} pair(s) "
                    f"from {stats.get('fingerprints', stats.get('tree', 0))} dHash(es)"
                )
                log_feed(
                    log,
                    logging.INFO,
                    'dHash near rebuild pairs=%s fps=%s ham=%s',
                    stats.get('pairs'),
                    stats.get('fingerprints', stats.get('tree')),
                    ham,
                )
                if self._duped_is_near():
                    self.duped_refresh()

            self._ui_schedule(done)

        threading.Thread(target=work, name='dhash-rebuild', daemon=True).start()

    def duped_mark_false_positive(self):
        if not self.store:
            return
        sel = list(self.duped_file_tree.selection())
        if not sel:
            messagebox.showinfo('Duped', 'Select one or more near-match rows.')
            return
        items = [self._duped_files[i] for i in sel if i in self._duped_files]
        near_items = [it for it in items if it.get('peer_sha1') or it.get('match_kind') == 'near']
        if not near_items:
            messagebox.showinfo(
                'Duped',
                'False positives apply to Near dHash matches.\n'
                'Switch to Near dHash and select rows.',
            )
            return
        if not messagebox.askyesno(
            'Duped',
            f'Mark {len(near_items)} pair(s) as false positive?\n'
            'They will not reappear after rebuild.',
        ):
            return
        n = 0
        for it in near_items:
            peer = it.get('peer_sha1')
            local = it.get('sha1')
            if not peer or not local:
                continue
            try:
                self.store.mark_dhash_false_positive(local, peer)
                n += 1
            except Exception as e:
                self.ui_log(f'FP mark failed: {e}')
        self.duped_status.set(f'Marked {n} false positive(s)')
        log_feed(log, logging.INFO, 'dHash false positives marked: %s', n)
        self.duped_refresh()

    def _on_duped_file_select(self, _event=None):
        """Sync board scroll to the selected tree row (board already shows all)."""
        if not self._lifecycle_alive:
            return
        sel = list(self.duped_file_tree.selection())
        if not sel:
            return
        iid = sel[0]
        # Cards are tagged with iid as widget name prefix card_<iid>
        inner = getattr(self, '_duped_compare_inner', None)
        canvas = getattr(self, '_duped_board_canvas', None)
        if inner is None or canvas is None:
            return
        for child in inner.winfo_children():
            if str(child).endswith(iid) or getattr(child, '_duped_iid', None) == iid:
                try:
                    canvas.yview_moveto(
                        max(0.0, child.winfo_y() / max(1, inner.winfo_height()))
                    )
                except Exception:
                    pass
                break

    def _duped_clear_compare(self):
        # Always tear down compare win; FP/Link set reopen flag to bring it back.
        self._duped_close_compare_win()
        inner = getattr(self, '_duped_compare_inner', None)
        if inner is None:
            return
        self._duped_thumb_gen += 1
        try:
            while True:
                self._duped_thumb_queue.get_nowait()
        except queue.Empty:
            pass
        with self._duped_thumb_ready_lock:
            self._duped_thumb_ready.clear()
        for child in inner.winfo_children():
            child.destroy()
        self._duped_compare_photos = []
        self._duped_board_items = []
        self._duped_board_loaded = 0
        self._duped_board_loading = False
        if len(self._duped_thumb_cache) > 4000:
            self._duped_thumb_cache.clear()

    def _duped_pair_linked(self, a: bytes | None, b: bytes | None) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        from image_dhash import order_sha_pair

        key = order_sha_pair(a, b)
        return key in self._duped_linked_pairs

    def _duped_rebuild_board(self, files: list[dict]):
        """Fill the scrollable match board (lazy + async thumbs)."""
        self._duped_clear_compare()
        inner = self._duped_compare_inner
        focus_key = self._duped_focus_key
        if not focus_key:
            ttk.Label(inner, text='Select a gallery.', foreground='#888').pack(
                anchor='w', padx=4, pady=8
            )
            return
        if not files:
            ttk.Label(
                inner, text='No matches listed for this gallery.', foreground='#888'
            ).pack(anchor='w', padx=4, pady=8)
            return
        try:
            self._duped_linked_pairs = (
                self.store.load_linked_sha_pairs() if self.store else set()
            )
        except Exception:
            self._duped_linked_pairs = set()
        try:
            self._duped_sequence(focus_key)
        except Exception:
            pass
        peer_warm: set[str] = set()
        for it in files[:40]:
            pk = self._duped_peer_key(it, focus_key)
            if pk and pk not in peer_warm:
                peer_warm.add(pk)
                try:
                    self._duped_sequence(pk)
                except Exception:
                    pass
                if len(peer_warm) >= 6:
                    break
        self._duped_board_items = list(files)
        self._duped_board_loaded = 0
        ttk.Label(
            inner,
            text=f'Loading match board… 0/{len(files)} (thumbs async)',
            foreground='#888888',
        ).pack(anchor='w', padx=4, pady=4)
        self._ui_schedule(self._duped_board_fill_viewport)
        self._ui_schedule(self._duped_maybe_reopen_compare)
        self.after(1, self._duped_board_load_more)

    def _duped_board_maybe_load_more(self):
        canvas = self._duped_board_canvas
        if canvas is None or self._duped_board_loading:
            return
        try:
            _top, bottom = canvas.yview()
        except Exception:
            return
        if bottom >= 0.85:
            self._duped_board_load_more()

    def _duped_board_fill_viewport(self):
        if not self._lifecycle_alive or self._duped_board_loading:
            return
        if self._duped_board_loaded >= len(self._duped_board_items):
            return
        canvas = self._duped_board_canvas
        if canvas is None:
            return
        try:
            bbox = canvas.bbox('all')
            if bbox is None:
                self._duped_board_load_more()
                return
            if bbox[3] < max(120, canvas.winfo_height() - 8):
                self._duped_board_load_more()
        except Exception:
            pass

    def _duped_board_load_more(self):
        if not self._lifecycle_alive or self._duped_board_loading:
            return
        items = self._duped_board_items
        start = self._duped_board_loaded
        if start >= len(items):
            return
        self._duped_board_loading = True
        end = min(len(items), start + DUPED_BOARD_BATCH)
        inner = self._duped_compare_inner
        for child in list(inner.winfo_children()):
            if isinstance(child, ttk.Label):
                txt = str(child.cget('text') or '')
                if txt.startswith('Loading match board'):
                    child.destroy()
        try:
            for i in range(start, end):
                self._duped_append_match_card(inner, items[i], index=i)
            self._duped_board_loaded = end
        finally:
            self._duped_board_loading = False
        canvas = self._duped_board_canvas
        if canvas is not None:
            try:
                canvas.configure(scrollregion=canvas.bbox('all'))
            except Exception:
                pass
        if end < len(items):
            self.after(20, self._duped_board_fill_viewport)
        else:
            self.duped_status.set(
                f'{self._duped_focus_key}: board complete ({end} cards)'
            )

    def _duped_close_compare_win(self) -> None:
        win = self._duped_compare_win
        self._duped_compare_win = None
        self._duped_cw_widgets = {}
        self._duped_compare_win_photos = []
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _duped_maybe_reopen_compare(self) -> None:
        idx = self._duped_compare_reopen_after_refresh
        self._duped_compare_reopen_after_refresh = None
        if idx is None or not self._lifecycle_alive:
            return
        items = self._duped_board_items
        if not items:
            return
        self._duped_open_compare(min(max(0, idx), len(items) - 1))

    def _on_duped_file_double(self, _event=None):
        sel = list(self.duped_file_tree.selection())
        if not sel:
            return
        iid = sel[0]
        for i, it in enumerate(self._duped_board_items or self._duped_files):
            if (it.get('sha1_hex') or '') == iid:
                self._duped_open_compare(i)
                return
        # Fallback: files list before board finished
        for i, it in enumerate(self._duped_files):
            if (it.get('sha1_hex') or '') == iid:
                if not self._duped_board_items:
                    self._duped_board_items = list(self._duped_files)
                self._duped_open_compare(i)
                return

    def _duped_open_compare(self, index: int) -> None:
        items = self._duped_board_items
        if not items:
            return
        index = max(0, min(int(index), len(items) - 1))
        self._duped_compare_idx = index
        win = self._duped_compare_win
        if win is None or not win.winfo_exists():
            self._duped_build_compare_win()
            win = self._duped_compare_win
        if win is None:
            return
        try:
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            pass
        self._duped_compare_show(index)

    def _duped_build_compare_win(self) -> None:
        self._duped_close_compare_win()
        win = tk.Toplevel(self)
        win.title('Duped compare')
        win.configure(background='#1e1e1e')
        try:
            win.geometry('1100x820')
            win.minsize(720, 520)
        except Exception:
            pass
        win.protocol('WM_DELETE_WINDOW', self._duped_close_compare_win)
        win.bind('<Escape>', lambda _e: self._duped_close_compare_win())
        win.bind('<Left>', lambda _e: self._duped_compare_nav(-1))
        win.bind('<Right>', lambda _e: self._duped_compare_nav(1))
        win.bind('<Prior>', lambda _e: self._duped_compare_nav(-1))
        win.bind('<Next>', lambda _e: self._duped_compare_nav(1))

        root = tk.Frame(win, bg='#1e1e1e', padx=8, pady=8)
        root.pack(fill='both', expand=True)

        title = tk.Label(
            root, text='', fg='#eee', bg='#1e1e1e', font=('Segoe UI', 11, 'bold')
        )
        title.pack(anchor='w')
        meta = tk.Label(
            root, text='', fg='#aaa', bg='#1e1e1e', font=('Segoe UI', 9), justify='left'
        )
        meta.pack(anchor='w', pady=(2, 6))

        nav = tk.Frame(root, bg='#1e1e1e')
        nav.pack(fill='x', pady=(0, 6))
        ttk.Button(nav, text='← Prev', width=10, command=lambda: self._duped_compare_nav(-1)).pack(
            side='left', padx=2
        )
        ttk.Button(nav, text='Next →', width=10, command=lambda: self._duped_compare_nav(1)).pack(
            side='left', padx=2
        )
        ttk.Button(
            nav, text='False positive', width=14, command=self._duped_compare_fp
        ).pack(side='left', padx=(16, 2))
        tk.Label(
            nav,
            text='←/→ navigate · Esc close',
            fg='#666',
            bg='#1e1e1e',
            font=('Segoe UI', 8),
        ).pack(side='right')

        panes = tk.Frame(root, bg='#1e1e1e')
        panes.pack(fill='both', expand=True)

        left_col = tk.Frame(
            panes,
            bg='#111',
            highlightbackground=DUPED_COLOR_LEFT,
            highlightthickness=3,
        )
        left_col.pack(side='left', fill='both', expand=True, padx=(0, 4))
        left_name = tk.Label(
            left_col,
            text='',
            fg=DUPED_COLOR_LEFT,
            bg='#111',
            font=('Segoe UI', 9, 'bold'),
            wraplength=500,
            justify='left',
        )
        left_name.pack(anchor='w', padx=6, pady=4)
        left_img = tk.Label(left_col, text='…', fg='#888', bg='#111')
        left_img.pack(fill='both', expand=True, padx=6, pady=6)

        right_col = tk.Frame(
            panes,
            bg='#111',
            highlightbackground=DUPED_COLOR_RIGHT,
            highlightthickness=3,
        )
        right_col.pack(side='left', fill='both', expand=True, padx=(4, 0))
        right_name = tk.Label(
            right_col,
            text='',
            fg=DUPED_COLOR_RIGHT,
            bg='#111',
            font=('Segoe UI', 9, 'bold'),
            wraplength=500,
            justify='left',
        )
        right_name.pack(anchor='w', padx=6, pady=4)
        right_img = tk.Label(right_col, text='…', fg='#888', bg='#111')
        right_img.pack(fill='both', expand=True, padx=6, pady=6)

        link_row = tk.Frame(root, bg='#1e1e1e')
        link_row.pack(fill='x', pady=(8, 0))
        tk.Label(
            link_row,
            text='Link neighbors:',
            fg='#888',
            bg='#1e1e1e',
            font=('Segoe UI', 8),
        ).pack(side='left')

        self._duped_compare_win = win
        self._duped_cw_widgets = {
            'title': title,
            'meta': meta,
            'left_name': left_name,
            'right_name': right_name,
            'left_img': left_img,
            'right_img': right_img,
            'link_row': link_row,
        }
        self._duped_compare_win_photos = []

    def _duped_compare_nav(self, delta: int) -> None:
        items = self._duped_board_items
        if not items:
            return
        self._duped_open_compare(self._duped_compare_idx + int(delta))

    def _duped_compare_fp(self) -> None:
        items = self._duped_board_items
        if not items:
            return
        idx = self._duped_compare_idx
        if 0 <= idx < len(items):
            self._duped_fp_one(items[idx])

    def _duped_compare_show(self, index: int) -> None:
        items = self._duped_board_items
        w = self._duped_cw_widgets
        if not items or not w or self._duped_compare_win is None:
            return
        index = max(0, min(index, len(items) - 1))
        self._duped_compare_idx = index
        item = items[index]
        focus_key = self._duped_focus_key or ''
        peer_key = self._duped_peer_key(item, focus_key)
        left_seq = self._duped_sequence(focus_key) if focus_key else []
        left_idx = self._duped_index_in_seq(left_seq, item, focus_key) if focus_key else None
        right_seq: list[dict] = []
        right_idx = None
        if peer_key:
            right_seq = self._duped_sequence(peer_key)
            right_idx = self._duped_index_in_seq(right_seq, item, peer_key)

        ham = item.get('hamming')
        kind = item.get('match_kind') or 'exact'
        if kind == 'manual' or ham == DUPED_MANUAL_HAMMING:
            dist_txt = 'manual link'
        elif ham is not None:
            dist_txt = f'Hamming {ham}'
        else:
            dist_txt = 'exact SHA'

        left_slot = left_seq[left_idx] if left_idx is not None and left_seq else {}
        right_slot = (
            right_seq[right_idx] if right_idx is not None and right_seq else {}
        )
        left_name = left_slot.get('name') or self._duped_alias_name(item, focus_key) or '?'
        right_name = (
            right_slot.get('name')
            or (self._duped_alias_name(item, peer_key) if peer_key else '')
            or '?'
        )
        left_path = left_slot.get('path')
        right_path = right_slot.get('path')

        try:
            w['title'].configure(
                text=f'#{index + 1} / {len(items)}  ·  {dist_txt}  ·  '
                f'{focus_key} ↔ {peer_key or "?"}'
            )
            w['meta'].configure(
                text=f'This: {left_path or "(missing)"}\nPeer: {right_path or "(missing)"}'
            )
            w['left_name'].configure(text=f'{focus_key}\n{left_name}')
            w['right_name'].configure(
                text=f'{peer_key or "?"}\n{right_name}' if peer_key else '(no peer)'
            )
            w['left_img'].configure(image='', text='…' if left_path else 'missing')
            w['right_img'].configure(image='', text='…' if right_path else 'missing')
        except Exception:
            return

        self._duped_compare_win_photos = []
        box = (DUPED_COMPARE_WIN_W, DUPED_COMPARE_WIN_H)
        if left_path is not None:
            self._duped_queue_thumb(w['left_img'], left_path, box, fast=False)
        if right_path is not None:
            self._duped_queue_thumb(w['right_img'], right_path, box, fast=False)

        # Rebuild Link neighbor buttons
        link_row = w['link_row']
        for child in list(link_row.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        tk.Label(
            link_row,
            text='Link neighbors:',
            fg='#888',
            bg='#1e1e1e',
            font=('Segoe UI', 8),
        ).pack(side='left')
        if (
            peer_key
            and left_idx is not None
            and right_idx is not None
        ):
            any_btn = False
            for off in (-3, -2, -1, 1, 2, 3):
                li = left_idx + off
                ri = right_idx + off
                if not (0 <= li < len(left_seq) and 0 <= ri < len(right_seq)):
                    continue
                sha_l = left_seq[li].get('sha1')
                sha_r = right_seq[ri].get('sha1')
                if not sha_l or not sha_r or sha_l == sha_r:
                    continue
                if self._duped_pair_linked(sha_l, sha_r):
                    ttk.Label(link_row, text=f'{off:+d}✓').pack(side='left', padx=2)
                    continue
                any_btn = True
                name_l = (left_seq[li].get('name') or '')[:24]
                name_r = (right_seq[ri].get('name') or '')[:24]
                ttk.Button(
                    link_row,
                    text=f'Link {off:+d}',
                    width=9,
                    command=lambda a=sha_l, b=sha_r, nl=name_l, nr=name_r: (
                        self._duped_manual_link(a, b, nl, nr)
                    ),
                ).pack(side='left', padx=2)
            if not any_btn:
                tk.Label(
                    link_row,
                    text='all neighbor offsets linked or missing',
                    fg='#666',
                    bg='#1e1e1e',
                    font=('Segoe UI', 8),
                ).pack(side='left', padx=4)
        else:
            tk.Label(
                link_row,
                text='(need both sequences for Link)',
                fg='#666',
                bg='#1e1e1e',
                font=('Segoe UI', 8),
            ).pack(side='left', padx=4)

        try:
            self._duped_compare_win.title(
                f'Duped compare — #{index + 1}/{len(items)} {dist_txt}'
            )
        except Exception:
            pass

    def _duped_append_match_card(self, parent, item: dict, *, index: int):
        focus_key = self._duped_focus_key or ''
        peer_key = self._duped_peer_key(item, focus_key)
        left_seq = self._duped_sequence(focus_key)
        left_idx = self._duped_index_in_seq(left_seq, item, focus_key)
        right_seq: list[dict] = []
        right_idx = None
        if peer_key:
            right_seq = self._duped_sequence(peer_key)
            right_idx = self._duped_index_in_seq(right_seq, item, peer_key)

        ham = item.get('hamming')
        kind = item.get('match_kind') or 'exact'
        if kind == 'manual' or ham == DUPED_MANUAL_HAMMING:
            dist_txt = 'manual'
        elif ham is not None:
            dist_txt = f'Hamming {ham}'
        else:
            dist_txt = 'exact SHA'

        card = tk.Frame(parent, bg='#252526', highlightbackground='#444', highlightthickness=1)
        card.pack(fill='x', padx=4, pady=6)
        card._duped_iid = item.get('sha1_hex') or ''  # type: ignore[attr-defined]
        card._duped_board_index = index  # type: ignore[attr-defined]

        head = tk.Frame(card, bg='#252526')
        head.pack(fill='x', padx=6, pady=4)
        tk.Label(
            head,
            text=f'#{index + 1}  {dist_txt}  ·  {focus_key} ↔ {peer_key or "?"}',
            fg='#ccc',
            bg='#252526',
            font=('Segoe UI', 9, 'bold'),
        ).pack(side='left')
        ttk.Button(
            head,
            text='Compare',
            width=10,
            command=lambda i=index: self._duped_open_compare(i),
        ).pack(side='right', padx=2)
        if item.get('peer_sha1') or kind in ('near', 'manual'):
            ttk.Button(
                head,
                text='False positive',
                width=14,
                command=lambda it=item: self._duped_fp_one(it),
            ).pack(side='right', padx=2)

        strips = tk.Frame(card, bg='#252526')
        strips.pack(fill='x', padx=4, pady=2)
        self._duped_build_board_strip(
            strips,
            gallery_key=focus_key,
            seq=left_seq,
            center_idx=left_idx,
            border=DUPED_COLOR_LEFT,
            title='This',
            photo_bucket=self._duped_compare_photos,
        )
        if peer_key:
            self._duped_build_board_strip(
                strips,
                gallery_key=peer_key,
                seq=right_seq,
                center_idx=right_idx,
                border=DUPED_COLOR_RIGHT,
                title='Peer',
                photo_bucket=self._duped_compare_photos,
            )
        else:
            tk.Label(
                strips, text='(no peer)', fg='#888', bg='#252526'
            ).pack(side='left', padx=12)

        # Neighbor Link buttons for same relative offset when not already linked.
        link_row = tk.Frame(card, bg='#252526')
        link_row.pack(fill='x', padx=6, pady=(0, 6))
        tk.Label(
            link_row,
            text='Link neighbors:',
            fg='#888',
            bg='#252526',
            font=('Segoe UI', 8),
        ).pack(side='left')
        if (
            peer_key
            and left_idx is not None
            and right_idx is not None
        ):
            any_btn = False
            for off in (-3, -2, -1, 1, 2, 3):
                li = left_idx + off
                ri = right_idx + off
                if not (0 <= li < len(left_seq) and 0 <= ri < len(right_seq)):
                    continue
                sha_l = left_seq[li].get('sha1')
                sha_r = right_seq[ri].get('sha1')
                if not sha_l or not sha_r or sha_l == sha_r:
                    continue
                if self._duped_pair_linked(sha_l, sha_r):
                    ttk.Label(link_row, text=f'±{off}✓').pack(side='left', padx=2)
                    continue
                any_btn = True
                name_l = (left_seq[li].get('name') or '')[:18]
                name_r = (right_seq[ri].get('name') or '')[:18]
                ttk.Button(
                    link_row,
                    text=f'Link {off:+d}',
                    width=9,
                    command=lambda a=sha_l, b=sha_r, nl=name_l, nr=name_r: (
                        self._duped_manual_link(a, b, nl, nr)
                    ),
                ).pack(side='left', padx=2)
            if not any_btn:
                tk.Label(
                    link_row,
                    text='all neighbor offsets already linked or missing',
                    fg='#666',
                    bg='#252526',
                    font=('Segoe UI', 8),
                ).pack(side='left', padx=4)
        else:
            tk.Label(
                link_row,
                text='(need both sequences to suggest neighbor Links)',
                fg='#666',
                bg='#252526',
                font=('Segoe UI', 8),
            ).pack(side='left', padx=4)

        # Bind wheel on card widgets
        canvas = self._duped_board_canvas

        def _wheel(event, c=canvas):
            if c is None:
                return
            c.yview_scroll(int(-event.delta / 120), 'units')
            self._duped_board_maybe_load_more()

        for w in (card, head, strips, link_row):
            w.bind('<MouseWheel>', _wheel)
            w.bind('<Double-1>', lambda _e, i=index: self._duped_open_compare(i))

        def _bind_dbl(widget, i=index):
            widget.bind('<Double-1>', lambda _e, idx=i: self._duped_open_compare(idx))
            for child in widget.winfo_children():
                _bind_dbl(child)

        _bind_dbl(strips)

    def _duped_build_board_strip(
        self,
        parent,
        *,
        gallery_key: str,
        seq: list[dict],
        center_idx: int | None,
        border: str,
        title: str,
        photo_bucket: list,
    ) -> None:
        """Horizontal strip of ±3 neighbors; thumbs load async."""
        col = tk.Frame(
            parent,
            bg='#1a1a1a',
            highlightbackground=border,
            highlightthickness=2,
        )
        col.pack(side='left', padx=4, pady=2, fill='y')
        tk.Label(
            col,
            text=f'{title}  {gallery_key}',
            fg=border,
            bg='#1a1a1a',
            font=('Segoe UI', 8, 'bold'),
        ).pack(pady=(2, 0))
        if center_idx is None:
            tk.Label(col, text='(no seq)', fg='#aaa', bg='#1a1a1a').pack(padx=6, pady=6)
            return
        row = tk.Frame(col, bg='#1a1a1a')
        row.pack(padx=2, pady=2)
        radius = DUPED_NEIGHBOR_RADIUS
        start = max(0, center_idx - radius)
        end = min(len(seq), center_idx + radius + 1)
        box = (DUPED_BOARD_THUMB_W, DUPED_BOARD_THUMB_H)
        for i in range(start, end):
            slot = seq[i]
            name = slot.get('name') or '?'
            path = slot.get('path')
            is_focus = i == center_idx
            cell_border = DUPED_COLOR_FOCUS if is_focus else border
            cell = tk.Frame(
                row,
                bg='#111',
                highlightbackground=cell_border,
                highlightthickness=3 if is_focus else 1,
            )
            cell.pack(side='left', padx=2, pady=2)
            lbl = tk.Label(
                cell,
                text='…' if path is not None else 'miss',
                width=8,
                height=5,
                fg='#888' if path is not None else '#f6ad55',
                bg='#111',
            )
            lbl.pack()
            if path is not None:
                self._duped_queue_thumb(lbl, path, box)
            tk.Label(
                cell,
                text=name[:16],
                fg='#eee' if is_focus else '#999',
                bg='#111',
                font=('Segoe UI', 6, 'bold' if is_focus else 'normal'),
            ).pack()

    def _duped_fp_one(self, item: dict):
        peer = item.get('peer_sha1')
        local = item.get('sha1')
        if not self.store or not peer or not local:
            return
        try:
            self.store.mark_dhash_false_positive(local, peer)
        except Exception as e:
            messagebox.showerror('Duped', f'False positive failed:\n{e}')
            return
        log_feed(log, logging.INFO, 'dHash FP one %s', (item.get('sha1_hex') or '')[:10])
        was_open = self._duped_compare_win is not None
        stay = self._duped_compare_idx
        if was_open:
            self._duped_compare_reopen_after_refresh = stay
        # Refresh list + board for current gallery
        self._on_duped_gallery_select()

    def _duped_manual_link(
        self, sha_a: bytes, sha_b: bytes, name_a: str, name_b: str
    ):
        if not self.store:
            return
        if not messagebox.askyesno(
            'Duped',
            f'Manually link neighbors?\n\n{name_a}\n↔\n{name_b}\n\n'
            'Stored as a near pair (source=manual). Survives Rebuild.',
        ):
            return
        try:
            self.store.add_manual_near_pair(sha_a, sha_b)
        except Exception as e:
            messagebox.showerror('Duped', f'Link failed:\n{e}')
            return
        from image_dhash import order_sha_pair

        self._duped_linked_pairs.add(order_sha_pair(sha_a, sha_b))
        self.duped_status.set(f'Linked {name_a} ↔ {name_b}')
        log_feed(log, logging.INFO, 'manual near link %s ↔ %s', name_a, name_b)
        was_open = self._duped_compare_win is not None
        if was_open:
            self._duped_compare_reopen_after_refresh = self._duped_compare_idx
        # Soft refresh board so Link buttons update; keep gallery selection.
        self._on_duped_gallery_select()

    def _duped_show_compare(self, item: dict):
        """Open large compare for this match item."""
        hex_id = item.get('sha1_hex') or ''
        for i, it in enumerate(self._duped_board_items or self._duped_files):
            if (it.get('sha1_hex') or '') == hex_id or it is item:
                self._duped_open_compare(i)
                return
        if item in (self._duped_files or []):
            self._duped_board_items = list(self._duped_files)
            self._duped_open_compare(self._duped_files.index(item))

    def duped_refresh(self):
        if not self.store:
            messagebox.showwarning('Duped', 'Database not ready.')
            return
        if self._duped_busy:
            messagebox.showinfo('Duped', 'Busy — wait for the current job.')
            return
        near = self._duped_is_near()
        try:
            if near:
                rows = self.store.list_near_dupe_galleries(limit=500)
            else:
                rows = self.store.list_dupe_galleries(
                    limit=500,
                    undecided_only=bool(self.duped_undecided_var.get()),
                )
        except Exception as e:
            messagebox.showerror('Duped', f'Query failed:\n{e}')
            return
        prev_sel = list(self.duped_gallery_tree.selection())
        self._duped_rows.clear()
        self._duped_seq_cache.clear()
        self._duped_clear_compare()
        self.duped_gallery_tree.delete(*self.duped_gallery_tree.get_children())
        self.duped_file_tree.delete(*self.duped_file_tree.get_children())
        self._duped_files.clear()
        for row in rows:
            key = row['gallery_key']
            folder = ''
            if row.get('out_dir'):
                folder = Path(row['out_dir']).name
            elif row.get('title'):
                folder = (row['title'] or '')[:80]
            if not folder:
                try:
                    names = self.store.list_gallery_ordered_names(key)
                except Exception:
                    names = []
                for a in names:
                    sp = a.get('sample_path')
                    if sp:
                        folder = Path(sp).parent.name
                        if folder:
                            row = dict(row)
                            row['out_dir'] = str(Path(sp).parent)
                            break
            iid = key
            self._duped_rows[iid] = row
            shared = row.get('undecided_count')
            if near or shared is None or not self.duped_undecided_var.get():
                shared = row.get('shared_count') or 0
            self.duped_gallery_tree.insert(
                '',
                'end',
                iid=iid,
                values=(
                    key,
                    str(shared),
                    str(row.get('peer_count') or 0),
                    folder,
                ),
            )
        col, rev = self._duped_gallery_sort
        self._duped_gallery_sort = (col, not rev)
        self._duped_sort_by('gallery', col)
        if near:
            try:
                stats = self.store.dhash_fill_stats()
                fill = f" · dHash {stats['filled']}/{stats['total']}"
            except Exception:
                fill = ''
            self.duped_status.set(
                f'{len(rows)} gallery(ies) with near matches{fill}'
            )
        else:
            mode = 'undecided' if self.duped_undecided_var.get() else 'all'
            self.duped_status.set(
                f'{len(rows)} gallery(ies) ({mode}) with shared files'
            )
        log_feed(
            log,
            logging.INFO,
            'Duped refresh (%s): %s gallery(ies)',
            'near' if near else 'exact',
            len(rows),
        )
        if prev_sel and prev_sel[0] in self._duped_rows:
            self.duped_gallery_tree.selection_set(prev_sel[0])
            self.duped_gallery_tree.focus(prev_sel[0])
            self._on_duped_gallery_select()

    def _on_duped_gallery_click(self, _event=None):
        # Ensure file list loads even if <<TreeviewSelect>> did not fire.
        self.after_idle(self._on_duped_gallery_select)

    def _on_duped_gallery_select(self, _event=None):
        self._duped_hide_preview()
        self._duped_clear_compare()
        self._duped_seq_cache.clear()
        sel = self.duped_gallery_tree.selection()
        if not sel or not self.store:
            return
        key = sel[0]
        self._duped_focus_key = key
        near = self._duped_is_near()
        undecided = bool(self.duped_undecided_var.get())
        try:
            if near:
                files = self.store.list_near_files_for_gallery(key)
            else:
                files = self.store.list_shared_files_for_gallery(
                    key,
                    undecided_only=undecided,
                )
        except Exception as e:
            log.exception('Duped detail failed for %s', key)
            self.ui_log(f'Duped detail failed: {e}')
            messagebox.showerror('Duped', f'Could not load shared files:\n{e}')
            return
        try:
            self.duped_file_tree.delete(*self.duped_file_tree.get_children())
        except tk.TclError:
            pass
        self._duped_files.clear()
        self._duped_file_pop_gen += 1
        gen = self._duped_file_pop_gen
        # Board first (lazy cards) so UI stays interactive while tree fills.
        self._duped_rebuild_board(files)
        if near:
            self.duped_status.set(
                f'{key}: loading {len(files)} near match(es)…'
            )
        else:
            mode = 'undecided' if undecided else 'all'
            self.duped_status.set(
                f'{key}: loading {len(files)} shared ({mode})…'
            )
        log.info('Duped gallery %s → %s file(s)', key, len(files))
        self._duped_populate_files_chunk(gen, key, near, undecided, files, 0)

    def _duped_populate_files_chunk(
        self,
        gen: int,
        key: str,
        near: bool,
        undecided: bool,
        files: list[dict],
        start: int,
    ):
        if (
            gen != self._duped_file_pop_gen
            or not self._lifecycle_alive
            or key != self._duped_focus_key
        ):
            return
        end = min(start + DUPED_TREE_CHUNK, len(files))
        try:
            for item in files[start:end]:
                digest = item['sha1']
                iid = digest.hex()
                local_name = self._duped_alias_name(item, key)
                peer_key = self._duped_peer_key(item, key)
                peer_name = (
                    self._duped_alias_name(item, peer_key) if peer_key else ''
                )
                peer_label = ''
                if peer_key:
                    peer_label = (
                        f'{peer_key}: {peer_name}' if peer_name else peer_key
                    )
                this_path = self._duped_display_path(key, local_name)
                peer_path = (
                    self._duped_display_path(peer_key, peer_name)
                    if peer_key
                    else ''
                )
                if near:
                    if item.get('match_kind') == 'manual' or item.get('hamming') == 255:
                        home_col = 'manual'
                    else:
                        home_col = str(item.get('hamming', ''))
                else:
                    home_col = item.get('home_gallery_key') or ''
                self._duped_files[iid] = item
                self.duped_file_tree.insert(
                    '',
                    'end',
                    iid=iid,
                    values=(
                        local_name or digest.hex()[:12],
                        this_path,
                        peer_label,
                        peer_path,
                        home_col,
                    ),
                )
        except Exception as e:
            log.exception('Duped file tree populate failed for %s', key)
            self.ui_log(f'Duped populate failed: {e}')
            return
        if end < len(files):
            self.after(
                1,
                lambda: self._duped_populate_files_chunk(
                    gen, key, near, undecided, files, end
                ),
            )
            return
        col, rev = self._duped_file_sort
        self._duped_file_sort = (col, not rev)
        self._duped_sort_by('file', col)
        if near:
            self.duped_status.set(
                f'{key}: {len(files)} near match(es) — double-click Compare window'
            )
        else:
            mode = 'undecided' if undecided else 'all'
            self.duped_status.set(
                f'{key}: {len(files)} shared ({mode}) — board + hover ±3'
            )

    def _duped_file_iids(self, *, scope: str) -> list[str]:
        """``scope`` is ``selected`` (tree selection) or ``all`` (all listed rows)."""
        if scope == 'all':
            return list(self.duped_file_tree.get_children())
        return list(self.duped_file_tree.selection())

    def duped_apply_home(self, *, scope: str = 'selected'):
        if not self.store:
            messagebox.showwarning('Duped', 'Database not ready.')
            return
        if self._duped_busy:
            messagebox.showinfo('Duped', 'Apply already running.')
            return
        gsel = self.duped_gallery_tree.selection()
        if not gsel:
            messagebox.showinfo('Duped', 'Select a home gallery on the left.')
            return
        home_key = gsel[0]
        home_row = self._duped_rows.get(home_key) or {}
        home_dir = home_row.get('out_dir')
        if not home_dir:
            gal = None
            try:
                gal = self.store.resolve_gallery_meta(home_key)
            except Exception:
                pass
            home_dir = (gal or {}).get('out_dir')
        if not home_dir:
            messagebox.showwarning(
                'Duped',
                f'No out_dir for gallery {home_key}. Complete/import it first.',
            )
            return

        fsel = self._duped_file_iids(scope=scope)
        if not fsel:
            if scope == 'selected':
                messagebox.showinfo(
                    'Duped',
                    'Select one or more files in the list, '
                    'or use Move to home → all listed.',
                )
            else:
                messagebox.showinfo('Duped', 'No shared files listed.')
            return

        items = [self._duped_files[i] for i in fsel if i in self._duped_files]
        create_links = bool(self.duped_links_var.get())
        n = len(items)
        scope_label = 'all listed' if scope == 'all' else 'selected'
        verb = 'move + link peers' if create_links else 'move without peer links'
        if not messagebox.askyesno(
            'Duped',
            f'Move to home {home_key} — {n} {scope_label} file(s)\n'
            f'({verb}).\n\n'
            f'Destination:\n{home_dir}',
        ):
            return

        self._duped_busy = True
        self._duped_stop.clear()
        self.duped_status.set(f'Applying home {home_key}…')

        def work():
            ok = 0
            fail = 0
            try:
                for item in items:
                    if self._duped_stop.is_set() or not self._lifecycle_alive:
                        break
                    try:
                        self._duped_appoint_one(
                            item,
                            home_key=home_key,
                            home_dir=Path(home_dir),
                            create_links=create_links,
                        )
                        ok += 1
                    except Exception as e:
                        fail += 1
                        self.ui_log(
                            f"Duped move fail {item.get('sha1_hex', '')[:10]}: {e}"
                        )
            finally:
                def done():
                    self._duped_busy = False
                    if not self._lifecycle_alive:
                        return
                    self.duped_status.set(
                        f'Home apply done — ok={ok} fail={fail}'
                    )
                    log_feed(
                        log,
                        logging.INFO,
                        'Duped home %s — ok=%s fail=%s links=%s scope=%s',
                        home_key,
                        ok,
                        fail,
                        create_links,
                        scope_label,
                    )
                    # Refresh lists so decided rows drop when filter is on.
                    self.duped_refresh()

                self._ui_schedule(done)

        threading.Thread(target=work, name='duped-apply', daemon=True).start()

    def duped_strip_peers(self, *, scope: str = 'selected'):
        """Remove peer symlinks/dup copies; keep the canonical home file only."""
        if not self.store:
            messagebox.showwarning('Duped', 'Database not ready.')
            return
        if self._duped_busy:
            messagebox.showinfo('Duped', 'Busy — wait for the current job.')
            return
        fsel = self._duped_file_iids(scope=scope)
        if not fsel:
            if scope == 'selected':
                messagebox.showinfo(
                    'Duped',
                    'Select one or more files, or use Strip peers → all listed.\n'
                    '(Uncheck Undecided only to see already-homed files.)',
                )
            else:
                messagebox.showinfo('Duped', 'No shared files listed.')
            return
        items = [self._duped_files[i] for i in fsel if i in self._duped_files]
        n = len(items)
        scope_label = 'all listed' if scope == 'all' else 'selected'
        if not messagebox.askyesno(
            'Duped',
            f'Strip peers for {n} {scope_label} file(s)?\n\n'
            'Deletes symlinks (and same-size duplicate copies) in other '
            'galleries. The home/real file is kept. DB aliases stay.',
        ):
            return

        self._duped_busy = True
        self._duped_stop.clear()
        self.duped_status.set('Stripping peer links…')

        def work():
            ok = 0
            fail = 0
            removed = 0
            try:
                for item in items:
                    if self._duped_stop.is_set() or not self._lifecycle_alive:
                        break
                    try:
                        removed += self._duped_strip_peers_one(item)
                        ok += 1
                    except Exception as e:
                        fail += 1
                        self.ui_log(
                            f"Duped strip fail {item.get('sha1_hex', '')[:10]}: {e}"
                        )
            finally:
                def done():
                    self._duped_busy = False
                    if not self._lifecycle_alive:
                        return
                    self.duped_status.set(
                        f'Strip peers done — files={ok} fail={fail} '
                        f'removed={removed}'
                    )
                    log_feed(
                        log,
                        logging.INFO,
                        'Duped strip peers — ok=%s fail=%s removed=%s scope=%s',
                        ok,
                        fail,
                        removed,
                        scope_label,
                    )
                    self._duped_seq_cache.clear()
                    self.duped_refresh()

                self._ui_schedule(done)

        threading.Thread(target=work, name='duped-strip', daemon=True).start()

    def _duped_strip_peers_one(self, item: dict) -> int:
        """Remove peer paths for one SHA. Returns number of paths removed."""
        aliases = item.get('aliases') or []
        real = resolve_real_file(item.get('sample_path'))
        if real is None:
            for a in aliases:
                real = resolve_real_file(a.get('sample_path'))
                if real is not None:
                    break
        if real is None:
            raise FileNotFoundError(
                'no on-disk home copy for ' + (item.get('sha1_hex') or '')[:12]
            )

        home_key = (item.get('home_gallery_key') or '').strip()
        removed = 0
        for a in aliases:
            gkey = (a.get('gallery_key') or '').strip()
            if not gkey or (home_key and gkey == home_key):
                continue
            gal = None
            try:
                gal = self.store.resolve_gallery_meta(gkey)
            except Exception:
                gal = None
            out = (gal or {}).get('out_dir')
            if not out:
                continue
            peer_name = a.get('name') or a.get('bare_name')
            if not peer_name:
                continue
            peer_path = Path(out) / peer_name
            status = strip_peer_presence(peer_path, real_keep=real)
            if status in ('link', 'dup'):
                removed += 1
        return removed

    def _duped_appoint_one(
        self,
        item: dict,
        *,
        home_key: str,
        home_dir: Path,
        create_links: bool,
    ) -> None:
        digest = item['sha1']
        aliases = item.get('aliases') or []
        # Preferred name in home gallery.
        home_name = None
        for a in aliases:
            if a.get('gallery_key') == home_key:
                home_name = a.get('name') or a.get('bare_name')
                if home_name:
                    break
        if not home_name:
            home_name = Path(item.get('sample_path') or 'file.bin').name

        real = resolve_real_file(item.get('sample_path'))
        if real is None:
            # Try any alias path.
            for a in aliases:
                real = resolve_real_file(a.get('sample_path'))
                if real is not None:
                    break
        if real is None:
            raise FileNotFoundError('no on-disk copy for ' + digest.hex()[:12])

        dest = home_dir / home_name
        # same_path() follows symlinks — a home-folder symlink to the real
        # bytes must still be replaced. Compare directory entries, not targets.
        if same_entry(real, dest):
            moved = Path(real)
        else:
            moved = move_real_file(real, dest)

        self.store.set_fingerprint_home(
            digest,
            sample_path=str(moved),
            gallery_key=home_key,
        )

        # Peer sites: symlink or remove duplicate presence (aliases stay in DB).
        for a in aliases:
            gkey = a.get('gallery_key') or ''
            if not gkey or gkey == home_key:
                continue
            gal = self.store.resolve_gallery_meta(gkey)
            out = (gal or {}).get('out_dir')
            if not out:
                continue
            peer_name = a.get('name') or a.get('bare_name') or home_name
            peer_path = Path(out) / peer_name
            if create_links:
                status = ensure_symlink(peer_path, moved)
                if status == 'exists_real' and not same_path(peer_path, moved):
                    try:
                        peer_path.unlink()
                        ensure_symlink(peer_path, moved)
                    except OSError as e:
                        self.ui_log(f'  peer replace failed {peer_path.name}: {e}')
            else:
                remove_path_if_link_or_dup(peer_path, real_keep=moved)

    # --- Import tab ---

    def _import_selected_iids(self) -> list[str]:
        return list(self.import_tree.selection())

    def _on_import_select(self, _event=None):
        iids = self._import_selected_iids()
        if len(iids) != 1:
            return
        row = self._import_rows.get(iids[0])
        if not row:
            return
        url = row.get('match_url') or row.get('url') or ''
        if url:
            self.import_url_var.set(url)

    def _import_row_values(self, row: dict) -> tuple:
        match = ''
        score = ''
        if row.get('match_title') or row.get('match_key'):
            key = row.get('match_key') or ''
            title = (row.get('match_title') or '')[:80]
            ver = row.get('match_verify')
            match = f'{key} {title}'.strip()
            if ver:
                match = f'{match} [{ver}]'.strip()
            sc = row.get('match_score')
            if sc is not None:
                score = f'{float(sc):.2f}'
        elif row.get('search_error'):
            match = f"err: {row['search_error'][:60]}"
        elif row.get('searched') and not row.get('match_key'):
            match = '(no confirmed hit)'
        return (
            row.get('name') or '',
            str(row.get('files') or 0),
            'yes' if row.get('in_galleries') else '—',
            'yes' if row.get('in_queue') else '—',
            match,
            score,
        )

    def _import_refresh_row(self, iid: str):
        row = self._import_rows.get(iid)
        if not row or not self._lifecycle_alive:
            return
        try:
            self.import_tree.item(iid, values=self._import_row_values(row))
        except tk.TclError:
            pass

    def import_scan(self):
        if self._import_busy:
            messagebox.showinfo('Import', 'Search/import still running — stop first.')
            return
        root = Path(self.dir_var.get().strip() or DEFAULT_DIR)
        if not root.is_dir():
            messagebox.showwarning('Import', f'Folder not found:\n{root}')
            return
        for iid in self.import_tree.get_children():
            self.import_tree.delete(iid)
        self._import_rows.clear()

        self._import_busy = True
        self._import_stop.clear()
        self.import_status.set('Scanning Save-to (extract archives if needed)…')
        threading.Thread(
            target=self._import_scan_worker,
            args=(root,),
            daemon=True,
        ).start()

    def _import_scan_worker(self, root: Path):
        ext_stats: dict = {}
        rows: list[dict] = []
        try:
            def _progress(msg: str):
                if self._lifecycle_alive:
                    self._ui_schedule(lambda m=msg: self.import_status.set(m))

            ext_stats = extract_toplevel_archives(
                root,
                progress=_progress,
                should_stop=lambda: (
                    not self._lifecycle_alive or self._import_stop.is_set()
                ),
            )
            for err in ext_stats.get('errors') or []:
                self.ui_log(f'Import archive: {err}')
            if ext_stats.get('extracted'):
                self.ui_log(
                    f"Import extracted {ext_stats['extracted']} archive(s) "
                    f"(removed={ext_stats.get('removed', 0)}, "
                    f"skipped={ext_stats.get('skipped', 0)}, "
                    f"failed={ext_stats.get('failed', 0)})"
                )

            if not self._lifecycle_alive or self._import_stop.is_set():
                return

            if self._lifecycle_alive:
                self._ui_schedule(lambda: self.import_status.set('Listing gallery folders…'))

            folders = scan_gallery_folders(root)
            for folder in folders:
                if not self._lifecycle_alive or self._import_stop.is_set():
                    return
                st = {}
                if self.store:
                    try:
                        st = self.store.local_folder_status(folder)
                    except Exception as e:
                        self.ui_log(f'Import status failed: {e}')
                files = list_images(folder)
                row = {
                    'path': str(folder),
                    'name': folder.name,
                    'files': len(files),
                    'in_galleries': bool(st.get('in_galleries')),
                    'in_queue': bool(st.get('in_queue')),
                    'url': st.get('url'),
                    'gallery_key': st.get('gallery_key'),
                    'match_key': None,
                    'match_token': None,
                    'match_url': None,
                    'match_title': None,
                    'match_score': None,
                    'match_hits': [],
                    'searched': False,
                    'search_error': None,
                }
                if st.get('gallery') or st.get('queue'):
                    src = st.get('gallery') or st.get('queue') or {}
                    row['match_key'] = src.get('gallery_key')
                    row['match_url'] = src.get('url')
                    row['match_title'] = src.get('title') or folder.name
                    row['match_score'] = 1.0
                rows.append(row)
        except Exception as e:
            self.ui_log(f'Import scan failed: {e}')
            log.exception('import scan failed: %s', e)
        finally:
            self._import_busy = False
            if self._lifecycle_alive:
                self._ui_schedule(lambda r=rows, rt=root, es=ext_stats: self._import_scan_apply(
                        r, rt, es
                    ),
                )

    def _import_scan_apply(self, rows: list[dict], root: Path, ext_stats: dict):
        if not self._lifecycle_alive:
            return
        for iid in self.import_tree.get_children():
            self.import_tree.delete(iid)
        self._import_rows.clear()
        for row in rows:
            iid = self.import_tree.insert('', 'end', values=self._import_row_values(row))
            self._import_rows[iid] = row

        extracted = int((ext_stats or {}).get('extracted') or 0)
        failed = int((ext_stats or {}).get('failed') or 0)
        msg = f'Scanned {len(rows)} gallery folder(s) under {root}'
        if extracted or failed:
            msg += f' (archives: extracted={extracted}, failed={failed})'
        if self._import_stop.is_set():
            msg += ' (stopped)'
        self.import_status.set(msg)
        self.ui_log(msg)
        log_feed(
            log,
            logging.INFO,
            'Import scan: %s folder(s) (extracted=%s failed=%s)',
            len(rows),
            extracted,
            failed,
        )

    def import_stop_search(self):
        self._import_stop.set()
        self.import_status.set('Stopping search…')

    def _import_ui_after_enqueue(self, url: str, iid: str):
        """UI-thread: listbox insert + import row refresh after search auto-queue."""
        if not self._lifecycle_alive:
            return
        key = gallery_key_from_url(url)
        if key and not any(gallery_key_from_url(u) == key for u in self._queue_urls):
            row = self._import_rows.get(iid) or {}
            title = (row.get('match_title') or row.get('name') or '').strip() or None
            total = row.get('match_image_total')
            try:
                total_i = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_i = None
            self._insert_queue_url(
                url, source='manual', title=title, image_total=total_i
            )
            if self._worker and self._worker.is_alive():
                self.job_queue.put(url)
            self._refresh_idle_status()
        self._import_refresh_row(iid)

    def _import_ask_sha_confirm(
        self, folder_name: str, candidates: list[dict]
    ) -> dict | None:
        """UI-thread: ask whether to accept an f_shash sample suggestion."""
        if not candidates or not self._lifecycle_alive:
            return None
        lines = []
        for i, c in enumerate(candidates[:5], 1):
            votes = c.get('votes')
            of = c.get('vote_of')
            title = (c.get('title') or '')[:90]
            lines.append(
                f"{i}. {c.get('gallery_key')}  "
                f"({votes}/{of} samples)  {title}"
            )
        best = candidates[0]
        msg = (
            f'No title match for:\n{folder_name}\n\n'
            f'SHA sample suggests:\n'
            + '\n'.join(lines)
            + f"\n\nAccept #1 ({best.get('gallery_key')}) as this folder?"
        )
        if messagebox.askyesno('Import SHA match', msg):
            return best
        return None

    def _import_confirm_sha_on_ui(
        self, folder_name: str, candidates: list[dict]
    ) -> dict | None:
        """Marshal SHA confirm dialog to the UI thread; wait for answer."""
        box: dict = {'hit': None}
        done = threading.Event()

        def ask():
            try:
                if self._lifecycle_alive:
                    box['hit'] = self._import_ask_sha_confirm(
                        folder_name, candidates
                    )
            finally:
                done.set()

        try:
            self._ui_schedule(ask)
        except tk.TclError:
            return None
        while not done.wait(0.25):
            if not self._lifecycle_alive or self._import_stop.is_set():
                return None
        return box.get('hit')

    def import_search_selected(self):
        iids = self._import_selected_iids()
        if not iids:
            messagebox.showinfo('Import', 'Select one or more rows.')
            return
        self._start_import_search(iids)

    def import_search_unmatched(self):
        iids = [
            iid for iid, row in self._import_rows.items()
            if not row.get('in_galleries') and not row.get('in_queue')
        ]
        if not iids:
            messagebox.showinfo('Import', 'No unmatched folders to search/queue.')
            return
        self._start_import_search(iids)

    def _start_import_search(self, iids: list[str]):
        if self._import_busy:
            messagebox.showinfo('Import', 'Search already running.')
            return
        if not iids:
            return
        self._import_busy = True
        self._import_stop.clear()
        self.import_status.set(f'Searching EH titles… 0/{len(iids)}')
        threading.Thread(
            target=self._import_search_worker,
            args=(list(iids),),
            daemon=True,
        ).start()

    def _import_auto_enqueue_row(self, row: dict, iid: str) -> bool:
        """Enqueue a matched import row (DB + UI). Returns True if newly queued."""
        if not self.store:
            return False
        url = (row.get('match_url') or '').strip()
        key = row.get('match_key') or gallery_key_from_url(url)
        if not url or not key:
            return False
        try:
            if self.store.is_completed_key(key):
                row['in_galleries'] = True
                return False
            if self.store.is_queued_key(key):
                row['in_queue'] = True
                try:
                    self.store.set_gallery_meta(url, out_dir=row.get('path'))
                except Exception:
                    pass
                self._ui_schedule(lambda u=url, i=iid: self._import_ui_after_enqueue(u, i)
                )
                return False
            self.store.enqueue(
                url,
                source='manual',
                title=row.get('match_title') or row.get('name'),
            )
            self.store.set_gallery_meta(
                url,
                title=row.get('match_title') or row.get('name'),
                out_dir=row.get('path'),
            )
            row['in_queue'] = True
            self._ui_schedule(lambda u=url, i=iid: self._import_ui_after_enqueue(u, i))
            self.ui_log(f"Import queued: {key} ← {(row.get('name') or '')[:50]!r}")
            return True
        except Exception as e:
            self.ui_log(f'Import auto-queue failed: {e}')
            return False

    def _import_search_worker(self, iids: list[str]):
        session = default_session()
        total = len(iids)
        done = 0
        queued = 0
        try:
            for i, iid in enumerate(iids):
                if not self._lifecycle_alive or self._import_stop.is_set():
                    break
                row = self._import_rows.get(iid)
                if not row:
                    continue
                name = row.get('name') or ''
                folder = Path(row.get('path') or '')

                # Already matched — re-verify against local files, then queue.
                if row.get('match_url') and row.get('match_key'):
                    try:
                        checked = verify_hit_against_folder(
                            session,
                            folder,
                            {
                                'gallery_key': row.get('match_key'),
                                'token': row.get('match_token'),
                                'url': row.get('match_url'),
                                'title': row.get('match_title'),
                                'score': row.get('match_score'),
                            },
                        )
                    except Exception as e:
                        row['search_error'] = str(e)
                        self.ui_log(f'Import verify error ({name[:40]}): {e}')
                        done += 1
                        try:
                            self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                        except tk.TclError:
                            break
                        continue
                    row['match_verify'] = checked.get('verify')
                    if checked.get('title'):
                        row['match_title'] = checked['title']
                    if checked.get('image_total'):
                        row['match_image_total'] = checked['image_total']
                    if not checked.get('verified'):
                        self.ui_log(
                            f"Import verify reject: {name[:50]!r} — "
                            f"{checked.get('verify')}"
                        )
                        row['match_key'] = None
                        row['match_url'] = None
                        row['match_title'] = None
                        row['match_score'] = None
                    elif self._import_auto_enqueue_row(row, iid):
                        queued += 1
                        self.ui_log(
                            f"Import verified {row['match_key']}: "
                            f"{checked.get('verify')}"
                        )
                    done += 1
                    try:
                        self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                    except tk.TclError:
                        break
                    continue

                try:
                    self._ui_schedule(lambda d=done, t=total, n=name[:50]: self.import_status.set(
                            f'Searching EH… {d}/{t} — {n}'
                        ),
                    )
                except tk.TclError:
                    break
                if i > 0:
                    # Space out folder searches.
                    t_end = time.monotonic() + SEARCH_INTERVAL
                    while time.monotonic() < t_end:
                        if not self._lifecycle_alive or self._import_stop.is_set():
                            break
                        time.sleep(0.15)
                if not self._lifecycle_alive or self._import_stop.is_set():
                    break
                try:
                    hits, query = search_by_folder_name(
                        session,
                        name,
                        folder=folder if folder.is_dir() else None,
                        interval=SEARCH_INTERVAL,
                        should_stop=lambda: (
                            not self._lifecycle_alive or self._import_stop.is_set()
                        ),
                    )
                    row['searched'] = True
                    row['search_error'] = None
                    row['match_hits'] = hits
                    if hits:
                        best = hits[0]
                        row['match_key'] = best.get('gallery_key')
                        row['match_token'] = best.get('token')
                        row['match_url'] = best.get('url')
                        row['match_title'] = best.get('title')
                        row['match_score'] = best.get('score')
                        row['match_verify'] = best.get('verify')
                        row['match_image_total'] = best.get('image_total')
                        self.ui_log(
                            f"Import match: {name[:60]!r} → {row['match_key']} "
                            f"[{best.get('verify') or 'title'}] (q={query!r})"
                        )
                        if self._import_auto_enqueue_row(row, iid):
                            queued += 1
                    else:
                        # Title miss → sample ~3 files via f_shash, ask user.
                        sha_hits: list[dict] = []
                        if folder.is_dir():
                            try:
                                self._ui_schedule(lambda n=name[:50]: self.import_status.set(
                                        f'SHA fallback… {n}'
                                    ),
                                )
                            except tk.TclError:
                                break
                            sha_hits = search_by_sample_shash(
                                session,
                                folder,
                                interval=SEARCH_INTERVAL,
                                should_stop=lambda: (
                                    not self._lifecycle_alive
                                    or self._import_stop.is_set()
                                ),
                            )
                        if sha_hits:
                            self.ui_log(
                                f"Import SHA candidates for {name[:50]!r}: "
                                + ', '.join(
                                    f"{h.get('gallery_key')}({h.get('votes')}/"
                                    f"{h.get('vote_of')})"
                                    for h in sha_hits[:5]
                                )
                            )
                            chosen = self._import_confirm_sha_on_ui(name, sha_hits)
                            if chosen and self._lifecycle_alive:
                                # Optional count/name enrich; user already confirmed.
                                try:
                                    checked = verify_hit_against_folder(
                                        session, folder, chosen
                                    )
                                    if checked.get('title'):
                                        chosen['title'] = checked['title']
                                    if checked.get('verify'):
                                        chosen['verify'] = (
                                            f"{chosen.get('verify')}; "
                                            f"{checked.get('verify')}"
                                        )
                                    chosen['image_total'] = checked.get(
                                        'image_total'
                                    )
                                except Exception as e:
                                    self.ui_log(
                                        f'Import SHA post-check: {e}'
                                    )
                                row['match_key'] = chosen.get('gallery_key')
                                row['match_token'] = chosen.get('token')
                                row['match_url'] = chosen.get('url')
                                row['match_title'] = chosen.get('title')
                                row['match_score'] = chosen.get('score')
                                row['match_verify'] = chosen.get('verify')
                                row['match_image_total'] = chosen.get(
                                    'image_total'
                                )
                                row['match_hits'] = sha_hits
                                self.ui_log(
                                    f"Import SHA accepted: {name[:50]!r} → "
                                    f"{row['match_key']} [{row.get('match_verify')}]"
                                )
                                if self._import_auto_enqueue_row(row, iid):
                                    queued += 1
                            else:
                                row['match_key'] = None
                                row['match_url'] = None
                                row['match_title'] = None
                                row['match_score'] = None
                                row['match_verify'] = None
                                row['match_hits'] = sha_hits
                                self.ui_log(
                                    f'Import SHA declined: {name[:60]!r}'
                                )
                        else:
                            row['match_key'] = None
                            row['match_url'] = None
                            row['match_title'] = None
                            row['match_score'] = None
                            row['match_verify'] = None
                            self.ui_log(
                                f'Import no confirmed hit: {name[:60]!r}'
                            )
                except Exception as e:
                    row['searched'] = True
                    row['search_error'] = str(e)
                    self.ui_log(f'Import search error ({name[:40]}): {e}')
                done += 1
                try:
                    self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                except tk.TclError:
                    break
        finally:
            self._import_busy = False
            if self._lifecycle_alive:
                try:
                    self._ui_schedule(lambda d=done, t=total, q=queued: self.import_status.set(
                            f'Search done — {d}/{t}, queued {q}'
                            + (' (stopped)' if self._import_stop.is_set() else '')
                        ),
                    )
                except tk.TclError:
                    pass

    def import_apply_url(self):
        url = self.import_url_var.get().strip()
        if not url or '/g/' not in url or not gallery_key_from_url(url):
            messagebox.showwarning('Import', 'Paste a valid e-hentai /g/… URL.')
            return
        iids = self._import_selected_iids()
        if not iids:
            messagebox.showinfo('Import', 'Select a folder row first.')
            return
        key = gallery_key_from_url(url)
        for iid in iids:
            row = self._import_rows.get(iid)
            if not row:
                continue
            row['match_url'] = url.split('#', 1)[0].split('?', 1)[0]
            row['match_key'] = key
            row['match_title'] = row.get('match_title') or row.get('name')
            row['match_score'] = 1.0
            row['searched'] = True
            if self.store:
                try:
                    row['in_galleries'] = self.store.is_completed_key(key)
                    row['in_queue'] = self.store.is_queued_key(key)
                except Exception:
                    pass
            self._import_refresh_row(iid)
        self.import_status.set(f'Applied URL to {len(iids)} row(s)')

    def import_selected(self):
        if not self.store:
            messagebox.showwarning('Import', 'Database not ready.')
            return
        if self._import_busy:
            messagebox.showinfo('Import', 'Wait for search to finish.')
            return
        iids = self._import_selected_iids()
        if not iids:
            messagebox.showinfo('Import', 'Select rows with an EH match.')
            return
        self._import_busy = True
        self._import_stop.clear()
        threading.Thread(
            target=self._import_do_worker,
            args=(list(iids),),
            daemon=True,
        ).start()

    def _import_do_worker(self, iids: list[str]):
        ok = 0
        skipped = 0
        try:
            for iid in iids:
                if not self._lifecycle_alive or self._import_stop.is_set():
                    break
                row = self._import_rows.get(iid)
                if not row:
                    continue
                url = (row.get('match_url') or '').strip()
                if not url or not gallery_key_from_url(url):
                    skipped += 1
                    self.ui_log(f"Import skip (no URL): {row.get('name')}")
                    continue
                key = gallery_key_from_url(url)
                try:
                    if self.store.is_completed_key(key):
                        skipped += 1
                        row['in_galleries'] = True
                        self.ui_log(f'Import skip (already in DB): {key}')
                        self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                        continue
                    if self.store.is_queued_key(key):
                        skipped += 1
                        row['in_queue'] = True
                        self.ui_log(f'Import skip (in queue): {key}')
                        self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                        continue
                    folder = Path(row['path'])
                    title = row.get('match_title') or row.get('name')
                    stats = import_local_gallery(
                        self.store,
                        folder,
                        url,
                        title=title,
                        fingerprint=True,
                    )
                    row['in_galleries'] = True
                    row['gallery_key'] = key
                    ok += 1
                    self.ui_log(
                        f"Imported {key} — fp={stats.get('fingerprinted', 0)} "
                        f"files={stats.get('image_count', 0)}"
                    )
                    log_feed(
                        log,
                        logging.INFO,
                        'Imported local gallery %s (%s files)',
                        key,
                        stats.get('image_count', 0),
                    )
                except Exception as e:
                    self.ui_log(f'Import failed {key}: {e}')
                    log.exception('import failed: %s', e)
                try:
                    self._ui_schedule(lambda i=iid: self._import_refresh_row(i))
                except tk.TclError:
                    break
        finally:
            self._import_busy = False
            if self._lifecycle_alive:
                try:
                    self._ui_schedule(lambda o=ok, s=skipped: self.import_status.set(
                            f'Import done — {o} imported, {s} skipped'
                        ),
                    )
                except tk.TclError:
                    pass

    def import_enqueue_selected(self):
        if not self.store:
            messagebox.showwarning('Import', 'Database not ready.')
            return
        iids = self._import_selected_iids()
        if not iids:
            messagebox.showinfo('Import', 'Select rows with an EH match.')
            return
        added = 0
        for iid in iids:
            row = self._import_rows.get(iid)
            if not row:
                continue
            url = (row.get('match_url') or '').strip()
            if not url or not gallery_key_from_url(url):
                continue
            key = gallery_key_from_url(url)
            if any(gallery_key_from_url(u) == key for u in self._queue_urls):
                row['in_queue'] = True
                self._import_refresh_row(iid)
                continue
            try:
                if self.store.is_queued(url):
                    row['in_queue'] = True
                    self._import_refresh_row(iid)
                    continue
                if self.store.is_completed(url):
                    if not messagebox.askyesno(
                        'Already in DB',
                        f'Gallery {key} is already completed.\nEnqueue to verify/fill gaps?',
                    ):
                        continue
                self.store.enqueue(
                    url,
                    source='manual',
                    title=row.get('match_title') or row.get('name'),
                )
                # Point queue meta at the local folder so download adopts it.
                try:
                    self.store.set_gallery_meta(
                        url,
                        title=row.get('match_title') or row.get('name'),
                        out_dir=row.get('path'),
                    )
                except Exception:
                    pass
            except Exception as e:
                self.ui_log(f'Enqueue failed: {e}')
                continue
            title = (row.get('match_title') or row.get('name') or '').strip() or None
            total = row.get('match_image_total')
            try:
                total_i = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_i = None
            self._insert_queue_url(
                url, source='manual', title=title, image_total=total_i
            )
            row['in_queue'] = True
            self._import_refresh_row(iid)
            added += 1
            if self._worker and self._worker.is_alive():
                self.job_queue.put(url)
        if added:
            log_feed(log, logging.INFO, 'Enqueued %s local match(es) for verify', added)
            self._refresh_idle_status()
            try:
                self._notebook.select(0)
            except tk.TclError:
                pass
        self.import_status.set(f'Enqueued {added} gallery(ies)')

    def _start_hash_worker(self):
        if not self.store or self._hash_worker:
            return
        saved = None
        try:
            saved = self.store.get_setting('eh_dupe_scan')
        except Exception:
            pass
        if saved is not None:
            self.eh_scan_var.set(saved not in ('0', 'false', 'False', 'no'))
        self._sync_eh_scan_enabled()
        self._hash_worker = EhHashCheckWorker(
            self.store,
            on_matches=self._on_eh_matches,
            on_status=self._on_eh_scan_status,
            lifecycle_alive=lambda: self._lifecycle_alive,
            # Pause f_shash while downloading — same IP budget as gallery traffic.
            enabled=lambda: self._eh_scan_enabled and not (
                self._worker and self._worker.is_alive()
            ),
        )
        self._hash_worker.start()

    def _sync_eh_scan_enabled(self):
        try:
            self._eh_scan_enabled = bool(self.eh_scan_var.get())
        except tk.TclError:
            pass

    def _on_eh_scan_toggle(self):
        self._sync_eh_scan_enabled()
        if self.store:
            try:
                self.store.set_setting(
                    'eh_dupe_scan',
                    '1' if self._eh_scan_enabled else '0',
                )
            except Exception:
                pass
        if self._eh_scan_enabled:
            self.ui_log('EH dupe scan enabled')
        else:
            self.ui_log('EH dupe scan paused')

    def _on_eh_scan_status(self, text: str):
        self._eh_scan_status = text or ''
        if not self._lifecycle_alive:
            return
        # Don't clobber an active download status line.
        if self._worker and self._worker.is_alive():
            return
        self._ui_schedule(lambda t=text: self._refresh_idle_status(t))

    def _refresh_idle_status(self, scan_text: str | None = None):
        if not self._lifecycle_alive:
            return
        if self._worker and self._worker.is_alive():
            return
        n = len(self._queue_urls)
        auto_n = sum(
            1 for u in self._queue_urls
            if self._queue_sources.get(u) == 'auto'
        )
        base = f'Idle — {n} in queue'
        if auto_n:
            base += f' ({auto_n} auto)'
        view_n = len(self._queue_view)
        if self._queue_filter_raw.strip() and view_n != n:
            base += f' · showing {view_n}'
        extra = scan_text if scan_text is not None else self._eh_scan_status
        if extra:
            base = f'{base} · {extra}'
        try:
            self.status.set(base)
        except tk.TclError:
            pass

    def _on_eh_matches(self, matches: list[dict]):
        """Called from hash-check thread — marshal to UI thread."""
        if not self._lifecycle_alive or not matches:
            return
        self._ui_schedule(lambda m=list(matches): self._auto_enqueue_matches(m))

    def _auto_enqueue_matches(self, matches: list[dict]):
        if not self._lifecycle_alive or not self.store:
            return
        added = 0
        for m in matches:
            url = (m.get('url') or '').strip()
            if not url or not gallery_key_from_url(url):
                continue
            key = gallery_key_from_url(url)
            if any(gallery_key_from_url(u) == key for u in self._queue_urls):
                continue
            title = (m.get('title') or '').strip() or None
            title = clean_search_hit_title(title)
            try:
                if self.store.is_completed(url) or self.store.is_queued(url):
                    continue
                self.store.enqueue(url, source='auto', title=title)
            except Exception as e:
                self.ui_log(f'Auto-queue failed: {e}')
                continue
            self._insert_queue_url(url, source='auto', title=title)
            added += 1
            if self._worker and self._worker.is_alive():
                self.job_queue.put(url)
            shown = (title or '')[:60]
            self.ui_log(
                f'  auto-queued {key}' + (f' — {shown}' if shown else '')
            )
        if added:
            log_feed(log, logging.INFO, 'Auto-queued %s gallery(ies) from EH hash', added)
            self._refresh_idle_status()

    def _queue_label(self, url: str) -> str:
        """Listbox text: gallery name (+ image count) alongside URL when known."""
        title = (self._queue_titles.get(url) or '').strip()
        total = self._queue_totals.get(url)
        count_bit = f' ({total})' if isinstance(total, int) and total > 0 else ''
        if not title:
            base = f'{url}{count_bit}' if count_bit else url
        else:
            shown = title if len(title) <= 80 else title[:77] + '…'
            base = f'{shown}{count_bit}  |  {url}'
        if url == self._current_url:
            return f'▶ {base}'
        return base

    def _remember_queue_title(self, url: str, title: str | None):
        t = clean_search_hit_title(title) or ''
        if t:
            self._queue_titles[url] = t
        elif url not in self._queue_titles:
            self._queue_titles[url] = ''

    def _remember_queue_total(self, url: str, image_total: int | None):
        if image_total is None:
            if url not in self._queue_totals:
                self._queue_totals[url] = None
            return
        try:
            n = int(image_total)
        except (TypeError, ValueError):
            return
        if n > 0:
            self._queue_totals[url] = n

    @staticmethod
    def _parse_queue_filter(raw: str) -> dict:
        """Parse filter text into name needles + image-count predicates.

        Examples: ``foo bar``, ``name:"exact"``, ``images:42``, ``images:>=40``.
        """
        text = (raw or '').strip()
        names: list[str] = []
        images: list[tuple[str, int]] = []
        if not text:
            return {'names': names, 'images': images}
        # images:/n: ops first, then name:"…", then bare words
        token_re = re.compile(
            r'(?i)(?:(?:images|n)\s*:\s*(>=|<=|>|<|=)?\s*(\d+))'
            r'|(?:name\s*:\s*"([^"]*)")'
            r'|(?:name\s*:\s*(\S+))'
            r'|("([^"]*)")'
            r'|(\S+)'
        )
        for m in token_re.finditer(text):
            if m.group(2) is not None:
                op = (m.group(1) or '=').strip() or '='
                images.append((op, int(m.group(2))))
            elif m.group(3) is not None:
                if m.group(3).strip():
                    names.append(m.group(3).strip())
            elif m.group(4) is not None:
                names.append(m.group(4))
            elif m.group(6) is not None:
                if m.group(6).strip():
                    names.append(m.group(6).strip())
            elif m.group(7) is not None:
                names.append(m.group(7))
        return {'names': names, 'images': images}

    def _queue_item_matches(self, url: str, filt: dict) -> bool:
        names = filt.get('names') or []
        images = filt.get('images') or []
        if not names and not images:
            return True
        title = (self._queue_titles.get(url) or '').casefold()
        url_l = url.casefold()
        key = (gallery_key_from_url(url) or '').casefold()
        hay = f'{title} {url_l} {key}'
        for needle in names:
            if needle.casefold() not in hay:
                return False
        total = self._queue_totals.get(url)
        for op, n in images:
            if not isinstance(total, int):
                return False
            if op == '=' and total != n:
                return False
            if op == '>=' and not (total >= n):
                return False
            if op == '<=' and not (total <= n):
                return False
            if op == '>' and not (total > n):
                return False
            if op == '<' and not (total < n):
                return False
        return True

    def _filtered_queue_urls(self) -> list[str]:
        filt = self._parse_queue_filter(self._queue_filter_raw)
        if not filt['names'] and not filt['images']:
            return list(self._queue_urls)
        return [u for u in self._queue_urls if self._queue_item_matches(u, filt)]

    def _on_queue_filter_changed(self):
        if not getattr(self, 'listbox', None):
            return
        try:
            raw = self.queue_filter_var.get()
        except tk.TclError:
            return
        if raw == self._queue_filter_raw:
            return
        self._queue_filter_raw = raw
        self._redraw_queue_listbox()
        self._refresh_idle_status()

    def _clear_queue_filter(self):
        self.queue_filter_var.set('')
        self.queue_filter_entry.focus_set()

    def _selected_queue_urls(self) -> list[str]:
        out = []
        for i in self.listbox.curselection():
            if 0 <= i < len(self._queue_view):
                out.append(self._queue_view[i])
        return out

    def _on_queue_context(self, event):
        try:
            idx = self.listbox.nearest(event.y)
        except tk.TclError:
            return
        if idx < 0 or idx >= self.listbox.size():
            return
        if idx not in self.listbox.curselection():
            self.listbox.selection_clear(0, 'end')
            self.listbox.selection_set(idx)
            self.listbox.activate(idx)
        urls = self._selected_queue_urls()
        if not urls:
            return
        menu = self._queue_menu
        menu.delete(0, 'end')
        menu.add_command(
            label='Copy URL' + (f' ({len(urls)})' if len(urls) > 1 else ''),
            command=lambda u=list(urls): self._copy_queue_urls(u),
        )
        if len(urls) == 1:
            title = (self._queue_titles.get(urls[0]) or '').strip()
            if title:
                menu.add_command(
                    label='Copy title',
                    command=lambda t=title: self._copy_text(t),
                )
                menu.add_command(
                    label='Filter by this title',
                    command=lambda t=title: self._filter_queue_by_title(t),
                )
            total = self._queue_totals.get(urls[0])
            if isinstance(total, int) and total > 0:
                menu.add_command(
                    label=f'Filter images:{total}',
                    command=lambda n=total: self.queue_filter_var.set(f'images:{n}'),
                )
        menu.add_separator()
        menu.add_command(
            label='Search / filter…',
            command=self._focus_queue_filter,
        )
        menu.add_command(label='Remove selected', command=self.remove_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_text(self, text: str):
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except tk.TclError:
            pass

    def _copy_queue_urls(self, urls: list[str]):
        text = '\n'.join(urls)
        self._copy_text(text)
        n = len(urls)
        self.ui_log(f'Copied {n} URL(s) to clipboard')

    def _filter_queue_by_title(self, title: str):
        t = (title or '').strip()
        if not t:
            return
        if ' ' in t or '"' in t:
            self.queue_filter_var.set(f'name:"{t.replace(chr(34), "")}"')
        else:
            self.queue_filter_var.set(t)
        self.queue_filter_entry.focus_set()

    def _focus_queue_filter(self):
        self.queue_filter_entry.focus_set()
        self.queue_filter_entry.selection_range(0, 'end')

    def _insert_queue_url(
        self,
        url: str,
        *,
        source: str = 'manual',
        title: str | None = None,
        image_total: int | None = None,
        sync: bool = True,
    ):
        """Keep manuals ahead of autos; refresh filtered listbox view."""
        src = 'auto' if source == 'auto' else 'manual'
        self._remember_queue_title(url, title)
        self._remember_queue_total(url, image_total)
        if src == 'auto':
            self._queue_urls.append(url)
            self._queue_sources[url] = 'auto'
        else:
            idx = next(
                (
                    i for i, u in enumerate(self._queue_urls)
                    if self._queue_sources.get(u) == 'auto'
                ),
                len(self._queue_urls),
            )
            self._queue_urls.insert(idx, url)
            self._queue_sources[url] = 'manual'
        if sync:
            self._redraw_queue_listbox(select_urls={url} if src == 'manual' else None)

    def _hydrate_from_store(self):
        if not self.store:
            self.ui_log('Waiting for EH database…')
            self.set_status('Idle — connecting to DB…')
            return
        try:
            saved_dir = self.store.get_setting('out_dir')
            if saved_dir:
                self.dir_var.set(saved_dir)
            saved_workers = self.store.get_setting('workers')
            if saved_workers:
                try:
                    self.workers_var.set(int(saved_workers))
                except ValueError:
                    pass
            restored = 0
            for item in self.store.list_active_queue():
                url = item['url']
                if item['status'] == 'running':
                    try:
                        self.store.mark_stopped(url)
                    except Exception:
                        pass
                if url in self._queue_urls:
                    continue
                src = item.get('source') or 'manual'
                title = (item.get('title') or '').strip() or None
                if not title:
                    key = item.get('gallery_key') or gallery_key_from_url(url)
                    try:
                        title = self.store.lookup_gallery_title(key) if key else None
                    except Exception:
                        title = None
                    if title:
                        try:
                            self.store.set_gallery_meta(url, title=title)
                        except Exception:
                            pass
                # Drop EH search tag chips so labels match on-disk folder names.
                cleaned = clean_search_hit_title(title)
                if cleaned and cleaned != (title or '').strip():
                    title = cleaned
                    try:
                        self.store.set_gallery_meta(url, title=cleaned)
                    except Exception:
                        pass
                elif cleaned:
                    title = cleaned
                total = item.get('image_total')
                try:
                    total_i = int(total) if total is not None else None
                except (TypeError, ValueError):
                    total_i = None
                self._insert_queue_url(
                    url,
                    source=src,
                    title=title,
                    image_total=total_i,
                    sync=False,
                )
                restored += 1
            if restored:
                self._redraw_queue_listbox()
            src = getattr(self.store, '_parts', {}).get('source', '?')
            self.ui_log(f'DB: EH ready (settings from {src})')
            if restored:
                self.ui_log(f'Restored {restored} queue item(s) from DB')
                log_feed(log, logging.INFO, 'Restored %s queue item(s)', restored)
            self._refresh_idle_status()
        except Exception as e:
            self.ui_log(f'DB hydrate failed: {e}')
            log.exception('hydrate failed: %s', e)

    def browse_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if path:
            self.dir_var.set(path)
            if self.store:
                try:
                    self.store.set_setting('out_dir', path)
                except Exception:
                    pass

    def add_url(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if '/g/' not in url:
            messagebox.showwarning('Invalid URL', 'Paste an e-hentai gallery URL (/g/...).')
            return
        if not gallery_key_from_url(url):
            messagebox.showwarning('Invalid URL', 'Could not parse gallery id from URL.')
            return
        if any(gallery_key_from_url(u) == gallery_key_from_url(url) for u in self._queue_urls):
            messagebox.showinfo('Queue', 'URL already in queue.')
            return
        if self.store:
            try:
                if self.store.is_completed(url):
                    if not messagebox.askyesno(
                        'Already downloaded',
                        'This gallery was already downloaded (recorded in EH DB).\n'
                        'Add to queue again anyway?',
                    ):
                        return
                self.store.enqueue(url, source='manual')
            except Exception as e:
                messagebox.showerror('Database', f'Could not enqueue:\n{e}')
                return
        self._insert_queue_url(url, source='manual')
        log_feed(log, logging.INFO, 'Queued %s', gallery_key_from_url(url) or url)
        if self._worker and self._worker.is_alive():
            # Manual should run before autos already waiting — rebuild is heavy;
            # put at front of remaining work by using a side note in status.
            self.job_queue.put(url)
            self.set_status(f'Running — {self.job_queue.qsize()} waiting (+ current)')
        self.url_var.set('')
        self.url_entry.focus_set()
        self._refresh_idle_status()

    def remove_selected(self):
        urls = self._selected_queue_urls()
        if not urls:
            return
        for url in urls:
            if self.store:
                try:
                    self.store.remove_by_url(url)
                except Exception as e:
                    self.ui_log(f'DB remove failed: {e}')
            try:
                self._queue_urls.remove(url)
            except ValueError:
                pass
            self._queue_sources.pop(url, None)
            self._queue_titles.pop(url, None)
            self._queue_totals.pop(url, None)
        self._redraw_queue_listbox()
        if self._worker and self._worker.is_alive():
            self._rebuild_waiting_jobs()
        self._refresh_idle_status()

    def queue_move(self, where: int | str):
        """Reorder selected queue rows. ``where``: -1/1 step, or ``top``/``bottom``."""
        sel_urls = self._selected_queue_urls()
        if not sel_urls:
            return
        indices = []
        for u in sel_urls:
            try:
                indices.append(self._queue_urls.index(u))
            except ValueError:
                pass
        if not indices:
            return
        indices = sorted(indices)
        n = len(self._queue_urls)
        if where == -1 and indices[0] == 0:
            return
        if where == 1 and indices[-1] == n - 1:
            return
        if where == 'top' and indices[0] == 0 and indices == list(range(len(indices))):
            return
        if where == 'bottom' and indices[-1] == n - 1 and indices == list(
            range(n - len(indices), n)
        ):
            return

        urls = self._queue_urls[:]
        block = [urls[i] for i in indices]
        for i in reversed(indices):
            del urls[i]
        if where == -1:
            insert_at = indices[0] - 1
        elif where == 1:
            insert_at = indices[0] + 1
        elif where == 'top':
            insert_at = 0
        else:
            insert_at = len(urls)
        for j, url in enumerate(block):
            urls.insert(insert_at + j, url)

        selected = set(block)
        self._queue_urls = urls
        self._redraw_queue_listbox(select_urls=selected)
        self._persist_queue_order()
        if self._worker and self._worker.is_alive():
            self._rebuild_waiting_jobs()
        self._refresh_idle_status()

    def _redraw_queue_listbox(self, *, select_urls: set[str] | None = None):
        select_urls = select_urls or set()
        view = self._filtered_queue_urls()
        self._queue_view = view
        self.listbox.delete(0, 'end')
        first_sel = None
        for i, url in enumerate(view):
            self.listbox.insert('end', self._queue_label(url))
            if url == self._current_url:
                fg = RUNNING_QUEUE_FG
            else:
                src = self._queue_sources.get(url, 'manual')
                fg = AUTO_QUEUE_FG if src == 'auto' else MANUAL_QUEUE_FG
            try:
                self.listbox.itemconfig(i, foreground=fg)
            except tk.TclError:
                pass
            if url in select_urls:
                self.listbox.selection_set(i)
                if first_sel is None:
                    first_sel = i
        if first_sel is None and self._current_url and self._current_url in view:
            try:
                first_sel = view.index(self._current_url)
                self.listbox.see(first_sel)
            except ValueError:
                first_sel = None
        if first_sel is not None:
            self.listbox.see(first_sel)
            self.listbox.activate(first_sel)

    def _persist_queue_order(self):
        if not self.store:
            return
        try:
            self.store.resequence(self._queue_urls)
        except Exception as e:
            self.ui_log(f'DB resequence failed: {e}')

    def _rebuild_waiting_jobs(self):
        """Refill ``job_queue`` from UI order (skip the gallery currently downloading)."""
        current = self._current_url
        while not self.job_queue.empty():
            try:
                self.job_queue.get_nowait()
            except queue.Empty:
                break
        for url in self._queue_urls:
            if current and url == current:
                continue
            self.job_queue.put(url)

    def clear_queue(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo('Busy', 'Stop the worker before clearing.')
            return
        if self.store:
            try:
                self.store.clear_queue()
            except Exception as e:
                messagebox.showerror('Database', f'Could not clear queue:\n{e}')
                return
        self.listbox.delete(0, 'end')
        self._queue_urls.clear()
        self._queue_sources.clear()
        self._queue_titles.clear()
        self._queue_totals.clear()
        self._queue_view = []
        self._refresh_idle_status()

    @staticmethod
    def _text_at_bottom(widget) -> bool:
        """True when the viewport already shows the end (tail-follow)."""
        try:
            return float(widget.yview()[1]) >= 0.999
        except (tk.TclError, TypeError, ValueError, IndexError):
            return True

    def ui_log(self, msg):
        if not self._lifecycle_alive:
            return

        def _append():
            if not self._lifecycle_alive:
                return
            try:
                follow = self._text_at_bottom(self.log_box)
                self.log_box.configure(state='normal')
                self.log_box.insert('end', msg + '\n')
                if follow:
                    self.log_box.see('end')
                self.log_box.configure(state='disabled')
            except tk.TclError:
                pass

        self._ui_schedule(_append)

    def set_status(self, text):
        if not self._lifecycle_alive:
            return
        self._ui_schedule(lambda: self.status.set(text))

    def start(self):
        if self._worker and self._worker.is_alive():
            return
        if not self._queue_urls:
            messagebox.showinfo('Queue', 'Add at least one gallery URL.')
            return
        out = self.dir_var.get().strip()
        if not out:
            messagebox.showwarning('Save to', 'Choose an output folder.')
            return
        Path(out).mkdir(parents=True, exist_ok=True)

        try:
            workers = int(self.workers_var.get())
        except (tk.TclError, ValueError):
            workers = DEFAULT_WORKERS
        workers = max(1, min(8, workers))

        if self.store:
            try:
                self.store.set_setting('out_dir', out)
                self.store.set_setting('workers', str(workers))
                self.store.resequence(self._queue_urls)
            except Exception as e:
                self.ui_log(f'DB settings/sync failed: {e}')

        self._stop.clear()
        while not self.job_queue.empty():
            try:
                self.job_queue.get_nowait()
            except queue.Empty:
                break
        for u in self._queue_urls:
            self.job_queue.put(u)

        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.set_status(f'Running — {self.job_queue.qsize()} in queue, {workers} workers')
        log_feed(
            log,
            logging.INFO,
            'Start — %s in queue, %s workers',
            self.job_queue.qsize(),
            workers,
        )
        self._worker = threading.Thread(
            target=self.worker, args=(out, workers), daemon=True
        )
        self._worker.start()

    def stop(self):
        self._stop.set()
        self.set_status('Stopping…')
        self.ui_log('Stop requested — cancelling workers…')
        log_feed(log, logging.INFO, 'Stop requested')

    def worker(self, out_dir, workers):
        done = 0
        try:
            while not self._stop.is_set() and self._lifecycle_alive:
                try:
                    url = self.job_queue.get(timeout=0.5)
                except queue.Empty:
                    break
                self._current_url = url
                self._ui_schedule(self._mark_queue_current, url)
                self.set_status(
                    f'Working… ({self.job_queue.qsize()} left, {workers} workers)'
                )
                self.ui_log(f'\nQueue item: {url}')
                log_feed(
                    log,
                    logging.INFO,
                    'Downloading gallery %s',
                    gallery_key_from_url(url) or url,
                )
                if self.store:
                    try:
                        # Do not pass Save-to root here — that would overwrite an
                        # Import folder path. parse_gallery sets the real out_dir.
                        self.store.mark_running(url)
                    except Exception as e:
                        self.ui_log(f'DB mark_running failed: {e}')
                dl = EHDownloader(
                    out_dir,
                    self.ui_log,
                    lambda: self._stop.is_set() or not self._lifecycle_alive,
                    workers=workers,
                    store=self.store,
                    gallery_url=url,
                    on_meta=lambda title=None, image_total=None, u=url: self._ui_schedule(lambda: self._update_running_queue_meta(
                            u, title=title, image_total=image_total
                        ),
                    ),
                )
                # Optional wake hook for near-pair incremental updates.
                dl._dhash_worker = self._dhash_worker
                try:
                    stats = dl.parse_gallery(url)
                    if self.store:
                        try:
                            self.store.complete_gallery(
                                url,
                                title=stats.get('title'),
                                out_dir=stats.get('target_dir'),
                                image_total=stats.get('total'),
                                saved=stats.get('saved', 0),
                                skipped=stats.get('skipped', 0),
                                failed=stats.get('failed', 0),
                            )
                            self.ui_log(
                                '  recorded in EH.galleries; temp queue rows removed'
                            )
                            log_feed(
                                log,
                                logging.INFO,
                                'Completed gallery %s (saved=%s skipped=%s failed=%s)',
                                gallery_key_from_url(url) or url,
                                stats.get('saved', 0),
                                stats.get('skipped', 0),
                                stats.get('failed', 0),
                            )
                        except Exception as e:
                            self.ui_log(f'DB complete failed: {e}')
                            log.exception('complete_gallery failed: %s', e)
                    done += 1
                    if self._lifecycle_alive:
                        self._ui_schedule(lambda u=url, s=stats: self._finish_queue_item(u, stats=s),
                        )
                except Exception as e:
                    # Hot-reload replaces DownloadStopped class identity; also treat
                    # stop/teardown as cancel, never as a failed gallery.
                    stopped = (
                        isinstance(e, DownloadStopped)
                        or type(e).__name__ == 'DownloadStopped'
                        or self._stop.is_set()
                        or not self._lifecycle_alive
                    )
                    if stopped:
                        self.ui_log('Stopped by user.')
                        log_feed(
                            log,
                            logging.INFO,
                            'Gallery stopped %s',
                            gallery_key_from_url(url) or url,
                        )
                        if self.store:
                            try:
                                self.store.mark_stopped(url)
                            except Exception as db_e:
                                self.ui_log(f'DB mark_stopped failed: {db_e}')
                        if self._lifecycle_alive:
                            self._ui_schedule(self._release_queue_current, url)
                        break
                    msg = str(e) or type(e).__name__
                    self.ui_log(f'Gallery error: {msg}')
                    log.exception('Gallery error for %s: %s', url, msg)
                    if self.store:
                        try:
                            self.store.mark_failed(url, msg)
                        except Exception as db_e:
                            self.ui_log(f'DB mark_failed failed: {db_e}')
                    if self._lifecycle_alive:
                        self._ui_schedule(self._release_queue_current, url)
        finally:
            if self._lifecycle_alive:
                self._ui_schedule(self._worker_done, done)

    def _mark_queue_current(self, url: str):
        """Keep the active gallery visible (▶ green) so URL can be copied."""
        if not self._lifecycle_alive:
            return
        # Refresh title/total from DB if parse already wrote meta (rare race).
        if self.store and url in self._queue_urls:
            key = gallery_key_from_url(url)
            try:
                q = self.store.find_queue_by_key(key) if key else None
            except Exception:
                q = None
            if q:
                if q.get('title'):
                    self._remember_queue_title(url, q.get('title'))
                if q.get('image_total'):
                    self._remember_queue_total(url, q.get('image_total'))
        self._redraw_queue_listbox(select_urls={url} if url in self._queue_urls else None)

    def _update_running_queue_meta(
        self,
        url: str,
        *,
        title: str | None = None,
        image_total: int | None = None,
    ):
        if not self._lifecycle_alive or url not in self._queue_urls:
            return
        self._remember_queue_title(url, title)
        self._remember_queue_total(url, image_total)
        self._redraw_queue_listbox(
            select_urls={url} if url == self._current_url else None
        )

    def _finish_queue_item(self, url: str, *, stats: dict | None = None):
        """Drop a completed gallery from the visible queue."""
        if not self._lifecycle_alive:
            return
        if self._current_url == url:
            self._current_url = None
        if stats:
            if stats.get('title'):
                self._remember_queue_title(url, stats.get('title'))
            if stats.get('total'):
                self._remember_queue_total(url, stats.get('total'))
        try:
            self._queue_urls.remove(url)
        except ValueError:
            pass
        self._queue_sources.pop(url, None)
        self._queue_titles.pop(url, None)
        self._queue_totals.pop(url, None)
        self._redraw_queue_listbox()

    def _release_queue_current(self, url: str):
        """Stop/fail: leave item in queue (front for manual) and clear ▶ mark."""
        if not self._lifecycle_alive:
            return
        if self._current_url == url:
            self._current_url = None
        if url not in self._queue_urls:
            # User removed it while it was running — do not restore.
            self._redraw_queue_listbox()
            return
        src = self._queue_sources.get(url, 'manual')
        self._queue_urls.remove(url)
        if src == 'auto':
            self._queue_urls.append(url)
        else:
            self._queue_urls.insert(0, url)
        self._redraw_queue_listbox(select_urls={url})

    def _worker_done(self, done):
        if not self._lifecycle_alive:
            return
        self._current_url = None
        self.start_btn.configure(state='normal')
        self.stop_btn.configure(state='disabled')
        if self._stop.is_set():
            self.set_status(
                f'Stopped — finished {done} gallery(ies), {len(self._queue_urls)} left'
            )
        else:
            self.set_status(f'Idle — finished {done} gallery(ies)')
        self.ui_log(f'\n=== batch finished ({done}) ===')
        log_feed(log, logging.INFO, 'Batch finished (%s gallery(ies))', done)
        self._redraw_queue_listbox()
