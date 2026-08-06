"""EH Gallery Queue UI shell (hot-reloadable).

Owned by ``eh_gallery_queue.HotReloadShell``. Ctrl+R reloads this module
and rebuilds :class:`App` without killing the process.

Tab UIs: ``import_tab.ImportTab``, ``duped_tab.DupedTab``.
Download engine: ``downloader.EHDownloader``.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from db import QueueStore, gallery_key_from_url
from downloader import DEFAULT_WORKERS, DownloadStopped, EHDownloader
from duped_tab import DupedTab
from eh_hash_check import EhHashCheckWorker, clean_search_hit_title
from import_tab import ImportTab
from logger import get_logger, log_feed

log = get_logger('app')

DEFAULT_DIR = r'a:\trt\.Pics'
# Listbox colors: manual (default) vs EH-discovered auto-queue vs currently parsing.
AUTO_QUEUE_FG = '#1565c0'
MANUAL_QUEUE_FG = '#000000'
RUNNING_QUEUE_FG = '#2e7d32'
# Cross-thread UI: workers never call Tk/after (Tcl lock stall on Windows).
UI_DRAIN_MS = 50
UI_DRAIN_BATCH = 40


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
        self.duped: DupedTab | None = None
        self.import_tab: ImportTab | None = None

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
        import_parent = ttk.Frame(nb)
        duped_parent = ttk.Frame(nb)
        nb.add(queue_tab, text='Queue')
        nb.add(import_parent, text='Import')
        nb.add(duped_parent, text='Duped')

        self._build_queue_tab(queue_tab)
        self.import_tab = ImportTab(import_parent, host=self)
        self.import_tab.pack(fill='both', expand=True)
        self.duped = DupedTab(duped_parent, host=self)
        self.duped.pack(fill='both', expand=True)

        self.status = tk.StringVar(value='Idle')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')

        self._hydrate_from_store()
        self._start_ui_drain()
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

    def _request_reload(self):
        shell = self.winfo_toplevel()
        if hasattr(shell, 'reload_app'):
            shell.reload_app()

    def prepare_for_reload(self):
        """Stop workers before the shell destroys this frame (WishAssistance-style)."""
        self._lifecycle_alive = False
        self._ui_drain_alive = False
        self._stop.set()
        if self.import_tab is not None:
            try:
                self.import_tab.shutdown()
            except Exception:
                pass
        with self._ui_pending_lock:
            self._ui_pending.clear()
        if self.duped is not None:
            try:
                self.duped.shutdown()
            except Exception:
                pass
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

        if self.duped is not None:
            self.duped.drain_thumb_ready()

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
                dl._dhash_worker = (
                    self.duped.dhash_worker if self.duped is not None else None
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
