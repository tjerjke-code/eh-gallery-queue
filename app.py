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
from eh_hash_check import EhHashCheckWorker, SEARCH_INTERVAL
from eh_title_search import default_session, search_by_folder_name
from local_import import import_local_gallery, list_images, scan_gallery_folders
from logger import get_logger, log_feed

log = get_logger('app')

DEFAULT_DIR = r'a:\trt\.Pics'
HEADERS = {'User-Agent': 'Mozilla/5.0'}
REQUEST_INTERVAL = 0.35
MAX_RETRIES = 5
DEFAULT_WORKERS = 4
WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Listbox colors: manual (default) vs EH-discovered auto-queue.
AUTO_QUEUE_FG = '#1565c0'
MANUAL_QUEUE_FG = '#000000'


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
        self._name_total = 1

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
        try:
            self.store.register_sha1(
                digest,
                byte_len,
                sample_path=str(path) if path else None,
                gallery_key=gallery_key_from_url(self.gallery_url or ''),
                name=name,
                bare_name=bare,
            )
        except Exception as e:
            self.log(f'  fp register failed: {e}')

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
        target_dir = Path(self.out_dir) / title
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
        self._current_url = None
        self._hash_worker: EhHashCheckWorker | None = None
        self._eh_scan_status = ''
        self._import_rows: dict[str, dict] = {}
        self._import_stop = threading.Event()
        self._import_busy = False

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
        nb.add(queue_tab, text='Queue')
        nb.add(import_tab, text='Import')

        self._build_queue_tab(queue_tab)
        self._build_import_tab(import_tab)

        self.status = tk.StringVar(value='Idle')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')

        self._hydrate_from_store()
        self._start_hash_worker()
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
        self.listbox = tk.Listbox(left, height=10, selectmode='extended')
        self.listbox.pack(fill='both', expand=True)
        btns = ttk.Frame(left)
        btns.pack(fill='x', pady=4)
        ttk.Button(btns, text='Remove', command=self.remove_selected).pack(side='left')
        ttk.Button(btns, text='Clear', command=self.clear_queue).pack(side='left', padx=4)

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
            text='Scan Save-to folders → search EH by title → import into DB / queue',
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

        self.import_status = tk.StringVar(value='Scan Save-to to list local galleries.')
        ttk.Label(parent, textvariable=self.import_status, padding=(8, 4)).pack(fill='x')

    def _request_reload(self):
        shell = self.winfo_toplevel()
        if hasattr(shell, 'reload_app'):
            shell.reload_app()

    def prepare_for_reload(self):
        """Stop workers before the shell destroys this frame (WishAssistance-style)."""
        self._lifecycle_alive = False
        self._stop.set()
        self._import_stop.set()
        hw = self._hash_worker
        self._hash_worker = None
        if hw:
            try:
                hw.stop(join_timeout=1.0)
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
            match = f'{key} {title}'.strip()
            sc = row.get('match_score')
            if sc is not None:
                score = f'{float(sc):.2f}'
        elif row.get('search_error'):
            match = f"err: {row['search_error'][:60]}"
        elif row.get('searched') and not row.get('match_key'):
            match = '(no hit)'
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

        folders = scan_gallery_folders(root)
        for folder in folders:
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
            # Pre-fill match from DB if known
            if st.get('gallery') or st.get('queue'):
                src = st.get('gallery') or st.get('queue') or {}
                row['match_key'] = src.get('gallery_key')
                row['match_url'] = src.get('url')
                row['match_title'] = src.get('title') or folder.name
                row['match_score'] = 1.0
            iid = self.import_tree.insert('', 'end', values=self._import_row_values(row))
            self._import_rows[iid] = row

        msg = f'Scanned {len(folders)} gallery folder(s) under {root}'
        self.import_status.set(msg)
        self.ui_log(msg)
        log_feed(log, logging.INFO, 'Import scan: %s folder(s)', len(folders))

    def import_stop_search(self):
        self._import_stop.set()
        self.import_status.set('Stopping search…')

    def import_search_selected(self):
        iids = self._import_selected_iids()
        if not iids:
            messagebox.showinfo('Import', 'Select one or more rows.')
            return
        self._start_import_search(iids)

    def import_search_unmatched(self):
        iids = [
            iid for iid, row in self._import_rows.items()
            if not row.get('in_galleries')
            and not row.get('match_key')
            and not row.get('in_queue')
        ]
        if not iids:
            messagebox.showinfo('Import', 'No unmatched folders to search.')
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

    def _import_search_worker(self, iids: list[str]):
        session = default_session()
        total = len(iids)
        done = 0
        try:
            for i, iid in enumerate(iids):
                if not self._lifecycle_alive or self._import_stop.is_set():
                    break
                row = self._import_rows.get(iid)
                if not row:
                    continue
                name = row.get('name') or ''
                try:
                    self.after(
                        0,
                        lambda d=done, t=total, n=name[:50]: self.import_status.set(
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
                        # Cross-check queue / DB by matched gid
                        if self.store and row['match_key']:
                            try:
                                key = row['match_key']
                                row['in_galleries'] = self.store.is_completed_key(key)
                                row['in_queue'] = self.store.is_queued_key(key)
                            except Exception:
                                pass
                        self.ui_log(
                            f"Import match: {name[:60]!r} → {row['match_key']} "
                            f"(q={query!r})"
                        )
                    else:
                        row['match_key'] = None
                        row['match_url'] = None
                        row['match_title'] = None
                        row['match_score'] = None
                        self.ui_log(f'Import no hit: {name[:60]!r}')
                except Exception as e:
                    row['searched'] = True
                    row['search_error'] = str(e)
                    self.ui_log(f'Import search error ({name[:40]}): {e}')
                done += 1
                try:
                    self.after(0, lambda i=iid: self._import_refresh_row(i))
                except tk.TclError:
                    break
        finally:
            self._import_busy = False
            if self._lifecycle_alive:
                try:
                    self.after(
                        0,
                        lambda d=done, t=total: self.import_status.set(
                            f'Search done — {d}/{t}'
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
                        self.after(0, lambda i=iid: self._import_refresh_row(i))
                        continue
                    if self.store.is_queued_key(key):
                        skipped += 1
                        row['in_queue'] = True
                        self.ui_log(f'Import skip (in queue): {key}')
                        self.after(0, lambda i=iid: self._import_refresh_row(i))
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
                    self.after(0, lambda i=iid: self._import_refresh_row(i))
                except tk.TclError:
                    break
        finally:
            self._import_busy = False
            if self._lifecycle_alive:
                try:
                    self.after(
                        0,
                        lambda o=ok, s=skipped: self.import_status.set(
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
                self.store.enqueue(url, source='manual')
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
            self._insert_queue_url(url, source='manual')
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
        self._hash_worker = EhHashCheckWorker(
            self.store,
            on_matches=self._on_eh_matches,
            on_status=self._on_eh_scan_status,
            lifecycle_alive=lambda: self._lifecycle_alive,
            enabled=lambda: bool(self.eh_scan_var.get()),
        )
        self._hash_worker.start()

    def _on_eh_scan_toggle(self):
        if self.store:
            try:
                self.store.set_setting(
                    'eh_dupe_scan',
                    '1' if self.eh_scan_var.get() else '0',
                )
            except Exception:
                pass
        if self.eh_scan_var.get():
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
        try:
            self.after(0, lambda t=text: self._refresh_idle_status(t))
        except tk.TclError:
            pass

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
        try:
            self.after(0, lambda m=list(matches): self._auto_enqueue_matches(m))
        except tk.TclError:
            pass

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
            try:
                if self.store.is_completed(url) or self.store.is_queued(url):
                    continue
                self.store.enqueue(url, source='auto')
            except Exception as e:
                self.ui_log(f'Auto-queue failed: {e}')
                continue
            self._insert_queue_url(url, source='auto')
            added += 1
            if self._worker and self._worker.is_alive():
                self.job_queue.put(url)
            title = (m.get('title') or '')[:60]
            self.ui_log(
                f'  auto-queued {key}' + (f' — {title}' if title else '')
            )
        if added:
            log_feed(log, logging.INFO, 'Auto-queued %s gallery(ies) from EH hash', added)
            self._refresh_idle_status()

    def _insert_queue_url(self, url: str, *, source: str = 'manual'):
        """Keep manuals ahead of autos in the listbox."""
        src = 'auto' if source == 'auto' else 'manual'
        if src == 'auto':
            self._queue_urls.append(url)
            self._queue_sources[url] = 'auto'
            self.listbox.insert('end', url)
            try:
                self.listbox.itemconfig('end', foreground=AUTO_QUEUE_FG)
            except tk.TclError:
                pass
            return
        idx = next(
            (
                i for i, u in enumerate(self._queue_urls)
                if self._queue_sources.get(u) == 'auto'
            ),
            len(self._queue_urls),
        )
        self._queue_urls.insert(idx, url)
        self._queue_sources[url] = 'manual'
        self.listbox.insert(idx, url)
        try:
            self.listbox.itemconfig(idx, foreground=MANUAL_QUEUE_FG)
        except tk.TclError:
            pass

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
                self._insert_queue_url(url, source=src)
                restored += 1
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
        for i in reversed(self.listbox.curselection()):
            url = self._queue_urls[i]
            if self.store:
                try:
                    self.store.remove_by_url(url)
                except Exception as e:
                    self.ui_log(f'DB remove failed: {e}')
            self.listbox.delete(i)
            del self._queue_urls[i]
            self._queue_sources.pop(url, None)
        self._refresh_idle_status()

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

        try:
            self.after(0, _append)
        except tk.TclError:
            pass

    def set_status(self, text):
        if not self._lifecycle_alive:
            return
        try:
            self.after(0, lambda: self.status.set(text))
        except tk.TclError:
            pass

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
                self.after(0, self._pop_queue_item, url)
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
                        self.store.mark_running(url, out_dir=out_dir)
                    except Exception as e:
                        self.ui_log(f'DB mark_running failed: {e}')
                dl = EHDownloader(
                    out_dir,
                    self.ui_log,
                    lambda: self._stop.is_set() or not self._lifecycle_alive,
                    workers=workers,
                    store=self.store,
                    gallery_url=url,
                )
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
                except DownloadStopped:
                    self.ui_log('Stopped by user.')
                    log_feed(log, logging.INFO, 'Gallery stopped %s', gallery_key_from_url(url) or url)
                    if self.store:
                        try:
                            self.store.mark_stopped(url)
                        except Exception as e:
                            self.ui_log(f'DB mark_stopped failed: {e}')
                    if self._lifecycle_alive:
                        self.after(0, self._requeue_front, url)
                    break
                except Exception as e:
                    self.ui_log(f'Gallery error: {e}')
                    log.exception('Gallery error for %s: %s', url, e)
                    if self.store:
                        try:
                            self.store.mark_failed(url, str(e))
                        except Exception as db_e:
                            self.ui_log(f'DB mark_failed failed: {db_e}')
                    if self._lifecycle_alive:
                        self.after(0, self._requeue_front, url)
                finally:
                    self._current_url = None
        finally:
            if self._lifecycle_alive:
                self.after(0, self._worker_done, done)

    def _pop_queue_item(self, url):
        if not self._lifecycle_alive:
            return
        try:
            i = self._queue_urls.index(url)
            del self._queue_urls[i]
            self.listbox.delete(i)
            # Keep _queue_sources so stop/fail can requeue with same color.
        except (ValueError, tk.TclError):
            pass

    def _requeue_front(self, url):
        if not self._lifecycle_alive:
            return
        if url in self._queue_urls:
            return
        src = self._queue_sources.get(url, 'manual')
        if src == 'auto':
            self._insert_queue_url(url, source='auto')
        else:
            self._queue_urls.insert(0, url)
            self._queue_sources[url] = 'manual'
            self.listbox.insert(0, url)
            try:
                self.listbox.itemconfig(0, foreground=MANUAL_QUEUE_FG)
            except tk.TclError:
                pass

    def _worker_done(self, done):
        if not self._lifecycle_alive:
            return
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
