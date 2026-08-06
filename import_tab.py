"""Import tab — scan Save-to, EH title search, register / enqueue."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from db import gallery_key_from_url
from eh_hash_check import SEARCH_INTERVAL
from eh_title_search import (
    default_session,
    search_by_folder_name,
    search_by_sample_shash,
    verify_hit_against_folder,
)
from local_import import (
    extract_toplevel_archives,
    import_local_gallery,
    list_images,
    scan_gallery_folders,
)
from logger import get_logger, log_feed

log = get_logger("import_tab")

DEFAULT_DIR = r"a:\trt\.Pics"


class ImportTab(ttk.Frame):
    """Local gallery import / EH match / enqueue UI."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host
        self._import_rows: dict[str, dict] = {}
        self._import_stop = threading.Event()
        self._import_busy = False
        self._build()

    @property
    def store(self):
        return self.host.store

    @property
    def dir_var(self):
        return self.host.dir_var

    @property
    def _lifecycle_alive(self) -> bool:
        return bool(self.host._lifecycle_alive)

    def ui_log(self, msg: str) -> None:
        self.host.ui_log(msg)

    def _ui_schedule(self, fn, *args) -> None:
        self.host._ui_schedule(fn, *args)

    def shutdown(self) -> None:
        self._import_stop.set()

    def _build(self):
        help_row = ttk.Frame(self, padding=(8, 6))
        help_row.pack(fill='x')
        ttk.Label(
            help_row,
            text='Scan Save-to → search EH by title → auto-queue matches (or Import into DB)',
        ).pack(side='left')

        tools = ttk.Frame(self, padding=(8, 0))
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
        tree_frame = ttk.Frame(self, padding=8)
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

        ov = ttk.Frame(self, padding=(8, 4))
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
        ttk.Label(self, textvariable=self.import_status, padding=(8, 4)).pack(fill='x')


    def _import_selected_iids(self) -> list[str]:
        return list(self.import_tree.selection())


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
        if key and not any(gallery_key_from_url(u) == key for u in self.host._queue_urls):
            row = self._import_rows.get(iid) or {}
            title = (row.get('match_title') or row.get('name') or '').strip() or None
            total = row.get('match_image_total')
            try:
                total_i = int(total) if total is not None else None
            except (TypeError, ValueError):
                total_i = None
            self.host._insert_queue_url(
                url, source='manual', title=title, image_total=total_i
            )
            if self.host._worker and self.host._worker.is_alive():
                self.host.job_queue.put(url)
            self.host._refresh_idle_status()
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
            if any(gallery_key_from_url(u) == key for u in self.host._queue_urls):
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
            self.host._insert_queue_url(
                url, source='manual', title=title, image_total=total_i
            )
            row['in_queue'] = True
            self._import_refresh_row(iid)
            added += 1
            if self.host._worker and self.host._worker.is_alive():
                self.host.job_queue.put(url)
        if added:
            log_feed(log, logging.INFO, 'Enqueued %s local match(es) for verify', added)
            self.host._refresh_idle_status()
            try:
                self._notebook.select(0)
            except tk.TclError:
                pass
        self.import_status.set(f'Enqueued {added} gallery(ies)')

