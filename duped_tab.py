"""Duped tab — Exact SHA / Near dHash review UI (hot-reloadable).

Owned by :class:`app.App` as a notebook page. Host provides store, Save-to
dir, UI drain scheduling, and lifecycle flag.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from dhash_fill import DhashFillWorker
from fs_links import (
    ensure_symlink,
    move_real_file,
    remove_path_if_link_or_dup,
    remove_peer_any,
    resolve_real_file,
    same_entry,
    same_path,
    strip_peer_presence,
)
from image_dhash import DEFAULT_MAX_HAMMING
from local_import import list_images, nat_key
from logger import get_logger, log_feed
from name_pattern import family_key, filter_seq_by_family
from set_siblings import suggest_set_siblings

log = get_logger("duped_tab")

DUPED_COLOR_LEFT = "#2B6CB0"
DUPED_COLOR_RIGHT = "#C05621"
DUPED_COLOR_FOCUS = "#ECC94B"
DUPED_COLOR_SIBLING = "#FFF3CD"
DUPED_COLOR_SIBLING_FG = "#856404"
DUPED_NEIGHBOR_RADIUS = 3
DUPED_THUMB_SIZE = 86
DUPED_COMPARE_SIZE = 220
DUPED_COMPARE_WIN_W = 520
DUPED_COMPARE_WIN_H = 700
DUPED_MANUAL_HAMMING = 255
DUPED_THUMB_APPLY_PER_TICK = 12
DUPED_THUMB_READY_MAX = 240
DUPED_TREE_CHUNK = 60
DUPED_MORE_THUMB = 140
DUPED_MORE_BATCH = 24


class DupedTab(ttk.Frame):
    """Exact/Near dupe review: gallery list + compare session window."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host

        self._duped_rows: dict[str, dict] = {}
        self._duped_files: dict[str, dict] = {}
        self._duped_file_list: list[dict] = []
        self._duped_stop = threading.Event()
        self._duped_busy = False
        self._duped_preview: tk.Toplevel | None = None
        self._duped_preview_photos: list = []
        self._duped_preview_iid: str | None = None
        self._duped_preview_after: str | None = None
        self._duped_preview_path: str | None = None
        self._duped_gallery_sort: tuple[str, bool] = ("shared", True)
        self._duped_file_sort: tuple[str, bool] = ("name", False)
        self._duped_focus_key: str | None = None
        self._duped_seq_cache: dict[str, list[dict]] = {}
        self._duped_preview_ctx: dict | None = None
        self._duped_mode = "exact"
        self._dhash_worker: DhashFillWorker | None = None
        self._duped_compare_photos: list = []
        self._duped_thumb_cache: dict[str, object] = {}
        self._duped_thumb_gen = 0
        self._duped_thumb_queue: queue.Queue = queue.Queue()
        self._duped_thumb_thread: threading.Thread | None = None
        self._duped_thumb_ready: deque = deque()
        self._duped_thumb_ready_lock = threading.Lock()
        self._duped_file_pop_gen = 0
        self._duped_compare_win: tk.Toplevel | None = None
        self._duped_compare_win_photos: list = []
        self._duped_cw_widgets: dict = {}
        self._duped_more_win: tk.Toplevel | None = None
        self._duped_more_photos: list = []
        # Compare session: parallel walk + staged Same/FP until Done.
        self._session: dict | None = None

        self._build()
        self.start_dhash_worker()

    # --- host bridges -------------------------------------------------------

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

    @property
    def dhash_worker(self) -> DhashFillWorker | None:
        return self._dhash_worker

    def shutdown(self) -> None:
        """Stop Duped workers before App frame destroy (Ctrl+R)."""
        self._duped_file_pop_gen += 1
        self._duped_stop.set()
        self._duped_hide_preview()
        self._duped_close_more_win()
        self._duped_close_compare_win()
        with self._duped_thumb_ready_lock:
            self._duped_thumb_ready.clear()
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

    def _build(self):
        help_row = ttk.Frame(self, padding=(8, 6))
        help_row.pack(fill='x')
        ttk.Label(
            help_row,
            text=(
                'Exact = SHA-1 (+ match groups). Near = dHash. '
                'Double-click row \u2192 compare session (Same / FP / Prev / Next / More / Done). '
                'Right-click \u2192 Explorer.'
            ),
        ).pack(side='left')

        tools = ttk.Frame(self, padding=(8, 0))
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

        exact_tools = ttk.Frame(self, padding=(8, 4))
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

        near_tools = ttk.Frame(self, padding=(8, 4))
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

        lists = ttk.Panedwindow(self, orient='horizontal')
        lists.pack(fill='both', expand=True, padx=8, pady=8)

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
            text='  (hover preview \u00b7 double-click \u2192 compare session)',
            foreground='#666666',
        ).pack(side='left', padx=8)
        tk.Label(
            legend,
            text='  set sibling?  ',
            fg=DUPED_COLOR_SIBLING_FG,
            bg=DUPED_COLOR_SIBLING,
            font=('Segoe UI', 8, 'bold'),
        ).pack(side='left', padx=(8, 0))

        tree_wrap = ttk.Frame(right)
        tree_wrap.pack(fill='both', expand=True)
        self.duped_file_tree = ttk.Treeview(
            tree_wrap,
            columns=fcols,
            show='headings',
            selectmode='extended',
            height=10,
        )
        self.duped_file_tree.tag_configure(
            'set_sibling',
            background=DUPED_COLOR_SIBLING,
            foreground=DUPED_COLOR_SIBLING_FG,
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
        self.duped_file_tree.bind('<Double-1>', self._on_duped_file_double)
        self._duped_file_menu = tk.Menu(self, tearoff=0)
        self.duped_gallery_tree.bind('<ButtonRelease-1>', self._on_duped_gallery_click)

        self.duped_status = tk.StringVar(
            value='Refresh to list galleries with shared fingerprints.'
        )
        ttk.Label(self, textvariable=self.duped_status, padding=(8, 4)).pack(fill='x')
        self._duped_apply_mode_ui()

    def drain_thumb_ready(self):
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
            if self._duped_more_win is not None:
                self._duped_more_photos.append(photo)
            try:
                if lbl.winfo_exists():
                    lbl.configure(image=photo, text='')
            except Exception:
                pass

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
        """Return PhotoImage or None (sync — prefer ``_duped_queue_thumb``)."""
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


    def start_dhash_worker(self):
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
            self.start_dhash_worker()
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


    @staticmethod
    def _session_pair_key(a: bytes | None, b: bytes | None) -> tuple[bytes, bytes] | None:
        if not a or not b:
            return None
        from image_dhash import order_sha_pair

        return order_sha_pair(a, b)

    def _duped_clear_compare(self):
        self._duped_close_more_win()
        self._duped_close_compare_win()
        self._session = None
        self._duped_thumb_gen += 1
        try:
            while True:
                self._duped_thumb_queue.get_nowait()
        except queue.Empty:
            pass
        with self._duped_thumb_ready_lock:
            self._duped_thumb_ready.clear()
        self._duped_compare_photos = []
        if len(self._duped_thumb_cache) > 4000:
            self._duped_thumb_cache.clear()

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

    def _duped_close_more_win(self) -> None:
        win = self._duped_more_win
        self._duped_more_win = None
        self._duped_more_photos = []
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _on_duped_file_double(self, _event=None):
        sel = list(self.duped_file_tree.selection())
        if not sel:
            return
        iid = sel[0]
        item = self._duped_files.get(iid)
        if not item:
            return
        if item.get('kind') == 'set_sibling':
            messagebox.showinfo(
                'Duped',
                'Set-sibling hint (no SHA peer).\n\n'
                f'{item.get("source_name") or "?"}\n'
                f'\u2192 {item.get("suggested_home_name") or "?"}\n'
                f'({item.get("sibling_reason") or "cluster gap"})\n\n'
                'Select the row and use Move to home to pull it into the '
                'dedicated set (preferred home).',
            )
            return
        self._duped_open_session(item)

    def _duped_show_compare(self, item: dict):
        """Open compare session for this match item."""
        self._duped_open_session(item)

    def _duped_open_session(self, item: dict) -> None:
        focus_key = self._duped_focus_key or ''
        if not focus_key:
            return
        peer_key = self._duped_peer_key(item, focus_key)
        if not peer_key:
            messagebox.showinfo('Duped', 'No peer gallery for this row.')
            return
        left_full = self._duped_sequence(focus_key)
        right_full = self._duped_sequence(peer_key)
        left_idx = self._duped_index_in_seq(left_full, item, focus_key)
        right_idx = self._duped_index_in_seq(right_full, item, peer_key)
        # Match-group peer may use a different SHA than the local row.
        peer_sha = item.get('peer_sha1')
        if right_idx is None and peer_sha and right_full:
            for i, slot in enumerate(right_full):
                if slot.get('sha1') == peer_sha:
                    right_idx = i
                    break
        if left_idx is None:
            messagebox.showinfo(
                'Duped',
                'Could not locate this file in the gallery sequence.',
            )
            return
        if right_idx is None:
            right_idx = (
                min(left_idx, max(0, len(right_full) - 1)) if right_full else 0
            )

        left_name = (left_full[left_idx].get('name') or '') if left_full else ''
        right_name = (
            (right_full[right_idx].get('name') or '') if right_full else ''
        )
        left_fam = family_key(left_name)
        right_fam = family_key(right_name)
        # Narrow walk to same set-id on each side when both names have a pattern.
        if left_fam and right_fam:
            walk_left = filter_seq_by_family(left_full, left_fam)
            walk_right = filter_seq_by_family(right_full, right_fam)
            pattern_mode = True
        else:
            walk_left = list(left_full)
            walk_right = list(right_full)
            pattern_mode = False
            left_fam = None
            right_fam = None

        def _idx_in_walk(walk: list[dict], full_idx: int, full: list[dict]) -> int:
            if not walk or not full or not (0 <= full_idx < len(full)):
                return 0
            target = full[full_idx]
            for i, slot in enumerate(walk):
                if slot is target or (
                    slot.get('name') == target.get('name')
                    and slot.get('sha1') == target.get('sha1')
                ):
                    return i
            # Fallback: match by name only.
            tname = target.get('name')
            for i, slot in enumerate(walk):
                if slot.get('name') == tname:
                    return i
            return 0

        wl = _idx_in_walk(walk_left, left_idx, left_full)
        wr = _idx_in_walk(walk_right, right_idx, right_full)

        self._session = {
            'focus_key': focus_key,
            'peer_key': peer_key,
            'walk_left': walk_left,
            'walk_right': walk_right,
            'left_idx': int(wl),
            'right_idx': int(wr),
            'anchor_left': int(wl),
            'anchor_right': int(wr),
            'nav_dir': 1,
            'pattern_mode': pattern_mode,
            'left_family': left_fam,
            'right_family': right_fam,
            'seed_item': item,
            'staged_same': set(),
            'staged_fp': set(),
            'more_pick_left': None,
            'more_pick_right': None,
        }
        self._duped_build_compare_win()
        self._duped_session_show()

    def _duped_build_compare_win(self) -> None:
        self._duped_close_more_win()
        self._duped_close_compare_win()
        win = tk.Toplevel(self)
        win.title('Duped compare session')
        win.configure(background='#1e1e1e')
        try:
            win.geometry('1100x820')
            win.minsize(720, 520)
        except Exception:
            pass
        win.protocol('WM_DELETE_WINDOW', self._duped_session_cancel)
        win.bind('<Escape>', lambda _e: self._duped_session_cancel())
        win.bind('<Left>', lambda _e: self._duped_session_nav(-1))
        win.bind('<Right>', lambda _e: self._duped_session_nav(1))
        win.bind('<Prior>', lambda _e: self._duped_session_nav(-1))
        win.bind('<Next>', lambda _e: self._duped_session_nav(1))
        win.bind('<Home>', lambda _e: self._duped_session_jump_end(-1))
        win.bind('<End>', lambda _e: self._duped_session_jump_end(1))
        win.bind('s', lambda _e: self._duped_session_mark_same())
        win.bind('S', lambda _e: self._duped_session_mark_same())
        win.bind('f', lambda _e: self._duped_session_mark_fp())
        win.bind('F', lambda _e: self._duped_session_mark_fp())

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
        status = tk.Label(
            root, text='', fg='#8f8', bg='#1e1e1e', font=('Segoe UI', 9, 'bold')
        )
        status.pack(anchor='w', pady=(0, 4))

        nav = tk.Frame(root, bg='#1e1e1e')
        nav.pack(fill='x', pady=(0, 6))
        ttk.Button(
            nav, text='<<', width=4, command=lambda: self._duped_session_jump_end(-1)
        ).pack(side='left', padx=2)
        ttk.Button(
            nav, text='← Prev', width=10, command=lambda: self._duped_session_nav(-1)
        ).pack(side='left', padx=2)
        ttk.Button(
            nav, text='Next →', width=10, command=lambda: self._duped_session_nav(1)
        ).pack(side='left', padx=2)
        ttk.Button(
            nav, text='>>', width=4, command=lambda: self._duped_session_jump_end(1)
        ).pack(side='left', padx=2)
        ttk.Button(
            nav, text='Same', width=10, command=self._duped_session_mark_same
        ).pack(side='left', padx=(16, 2))
        ttk.Button(
            nav, text='False positive', width=14, command=self._duped_session_mark_fp
        ).pack(side='left', padx=2)
        ttk.Button(
            nav, text='More…', width=10, command=self._duped_session_open_more
        ).pack(side='left', padx=(16, 2))
        ttk.Button(
            nav, text='Done', width=10, command=self._duped_session_done
        ).pack(side='left', padx=(16, 2))
        ttk.Button(
            nav, text='Cancel', width=10, command=self._duped_session_cancel
        ).pack(side='left', padx=2)
        tk.Label(
            nav,
            text='<< / >> ends · ←/→ walk · S same (skip matched) · F FP · Esc',
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

        self._duped_compare_win = win
        self._duped_cw_widgets = {
            'title': title,
            'meta': meta,
            'status': status,
            'left_name': left_name,
            'right_name': right_name,
            'left_img': left_img,
            'right_img': right_img,
        }
        self._duped_compare_win_photos = []

    def _duped_session_walks(self) -> tuple[list[dict], list[dict]]:
        sess = self._session
        if not sess:
            return [], []
        left = sess.get('walk_left')
        right = sess.get('walk_right')
        if left is None:
            left = self._duped_sequence(sess['focus_key'])
        if right is None:
            right = self._duped_sequence(sess['peer_key'])
        return left, right

    def _duped_session_current_shas(self) -> tuple[bytes | None, bytes | None]:
        sess = self._session
        if not sess:
            return None, None
        return self._duped_session_shas_at(sess['left_idx'], sess['right_idx'])

    def _duped_session_shas_at(
        self, left_idx: int, right_idx: int
    ) -> tuple[bytes | None, bytes | None]:
        left_seq, right_seq = self._duped_session_walks()
        sha_l = left_seq[left_idx].get('sha1') if 0 <= left_idx < len(left_seq) else None
        sha_r = (
            right_seq[right_idx].get('sha1') if 0 <= right_idx < len(right_seq) else None
        )
        return sha_l, sha_r

    def _duped_session_slice_left_range(self) -> tuple[int, int] | None:
        """Inclusive left-index range ``[lo, hi]`` where both walk sides stay in bounds."""
        sess = self._session
        if not sess:
            return None
        left_seq, right_seq = self._duped_session_walks()
        if not left_seq or not right_seq:
            return None
        offset = int(sess['anchor_left']) - int(sess['anchor_right'])
        # R = L - offset; need 0 <= L < lenL and 0 <= R < lenR
        lo = max(0, offset)
        hi_excl = min(len(left_seq), len(right_seq) + offset)
        if lo >= hi_excl:
            return None
        return lo, hi_excl - 1

    def _duped_session_nav(self, delta: int) -> bool:
        """Move both sides by ``delta`` within the walk slice. False at an edge."""
        sess = self._session
        if not sess:
            return False
        d = int(delta)
        if d:
            sess['nav_dir'] = 1 if d > 0 else -1
        left_seq, right_seq = self._duped_session_walks()
        if not left_seq and not right_seq:
            return False
        new_l = sess['left_idx'] + d
        new_r = sess['right_idx'] + d
        if left_seq and not (0 <= new_l < len(left_seq)):
            return False
        if right_seq and not (0 <= new_r < len(right_seq)):
            return False
        if left_seq:
            sess['left_idx'] = new_l
        if right_seq:
            sess['right_idx'] = new_r
        sess['more_pick_left'] = None
        sess['more_pick_right'] = None
        self._duped_session_show()
        return True

    def _duped_session_jump_end(self, which: int) -> None:
        """Jump to start (``which < 0``) or end (``which > 0``) of the aligned slice."""
        sess = self._session
        if not sess:
            return
        bounds = self._duped_session_slice_left_range()
        if bounds is None:
            return
        lo, hi = bounds
        offset = int(sess['anchor_left']) - int(sess['anchor_right'])
        target_l = lo if which < 0 else hi
        sess['left_idx'] = target_l
        sess['right_idx'] = target_l - offset
        sess['nav_dir'] = -1 if which < 0 else 1
        sess['more_pick_left'] = None
        sess['more_pick_right'] = None
        self._duped_session_show()

    def _duped_session_goto_unmatched(self, direction: int | None = None) -> bool:
        """Advance to the next pair not in staged Same, following walk direction."""
        sess = self._session
        if not sess:
            return False
        direction = int(direction if direction is not None else sess.get('nav_dir') or 1)
        if direction == 0:
            direction = 1
        sess['nav_dir'] = direction
        bounds = self._duped_session_slice_left_range()
        if bounds is None:
            return False
        lo, hi = bounds
        offset = int(sess['anchor_left']) - int(sess['anchor_right'])
        cur_l = int(sess['left_idx'])
        step = 1 if direction > 0 else -1
        # Start searching from the neighbor of the current (just-matched) slot.
        pos = cur_l + step
        while lo <= pos <= hi:
            ri = pos - offset
            sha_l, sha_r = self._duped_session_shas_at(pos, ri)
            key = self._session_pair_key(sha_l, sha_r)
            if key is None and sha_l and sha_r and sha_l == sha_r:
                key = (sha_l, sha_r)
            if key is None or key not in sess['staged_same']:
                sess['left_idx'] = pos
                sess['right_idx'] = ri
                sess['more_pick_left'] = None
                sess['more_pick_right'] = None
                self._duped_session_show()
                return True
            pos += step
        return False

    def _duped_session_mark_same(self) -> None:
        sess = self._session
        if not sess:
            return
        sha_l, sha_r = self._duped_session_current_shas()
        key = self._session_pair_key(sha_l, sha_r)
        if key is None:
            # Same SHA or missing: still stage identical digests as reviewed.
            if sha_l and sha_r and sha_l == sha_r:
                key = (sha_l, sha_r)
            elif sha_l and not sha_r:
                messagebox.showinfo('Duped', 'Peer slot has no SHA yet.')
                return
            elif sha_r and not sha_l:
                messagebox.showinfo('Duped', 'This slot has no SHA yet.')
                return
            else:
                messagebox.showinfo('Duped', 'Both sides need a file/SHA to mark Same.')
                return
        sess['staged_fp'].discard(key)
        sess['staged_same'].add(key)
        if self._duped_more_win is not None:
            self._duped_session_refresh_more()
        # Skip already-matched pairs in the current walk direction.
        if not self._duped_session_goto_unmatched():
            self._duped_session_show()

    def _duped_session_mark_fp(self) -> None:
        sess = self._session
        if not sess:
            return
        sha_l, sha_r = self._duped_session_current_shas()
        key = self._session_pair_key(sha_l, sha_r)
        if key is None and sha_l and sha_r and sha_l == sha_r:
            key = (sha_l, sha_r)
        if key is None:
            return
        if key in sess['staged_same']:
            sess['staged_same'].discard(key)
            self._duped_session_show()
            if self._duped_more_win is not None:
                self._duped_session_refresh_more()
            return
        sess['staged_fp'].add(key)
        self._duped_session_show()

    def _duped_session_cancel(self) -> None:
        self._duped_close_more_win()
        self._duped_close_compare_win()
        self._session = None

    def _duped_session_done(self) -> None:
        sess = self._session
        if not sess or not self.store:
            self._duped_session_cancel()
            return
        same_pairs = list(sess.get('staged_same') or [])
        fp_pairs = list(sess.get('staged_fp') or [])
        focus_key = sess['focus_key']
        peer_key = sess['peer_key']
        merged = 0
        fps = 0
        try:
            for a, b in same_pairs:
                if a == b:
                    continue
                if self.store.merge_sha1_match(a, b):
                    merged += 1
            for a, b in fp_pairs:
                if a == b:
                    continue
                try:
                    self.store.mark_dhash_false_positive(a, b)
                    fps += 1
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror('Duped', f'Done failed:\n{e}')
            return
        log_feed(
            log,
            logging.INFO,
            'compare session done: %s match merge(s), %s FP',
            merged,
            fps,
        )
        # Build rows from session Same pairs (include identical-SHA staged pairs).
        pair_rows = list(same_pairs)
        self._duped_session_cancel()
        # Exact tools (Move / Strip) apply to these rows.
        if self._duped_is_near():
            try:
                self.duped_mode_var.set('exact')
                self._duped_mode = 'exact'
                self._duped_apply_mode_ui()
            except Exception:
                pass
        # Show every session match — Undecided would hide already-decided homes.
        try:
            self.duped_undecided_var.set(False)
        except Exception:
            pass
        self._duped_focus_key = focus_key
        try:
            if focus_key in self._duped_rows:
                self.duped_gallery_tree.selection_set(focus_key)
                self.duped_gallery_tree.focus(focus_key)
                self.duped_gallery_tree.see(focus_key)
        except Exception:
            pass
        items = self._duped_items_from_session_pairs(focus_key, peer_key, pair_rows)
        self._duped_load_file_items(items)
        n = len(items)
        self.duped_status.set(
            f'Session done — {n} matched pair(s) listed. '
            f'Move to home (uncheck symlinks to delete peers) or Strip peers.'
        )
        if n:
            try:
                kids = list(self.duped_file_tree.get_children())
                if kids:
                    self.duped_file_tree.selection_set(kids)
                    self.duped_file_tree.focus(kids[0])
                    self.duped_file_tree.see(kids[0])
            except Exception:
                pass

    def _duped_items_from_session_pairs(
        self,
        focus_key: str,
        peer_key: str,
        pairs: list[tuple[bytes, bytes]],
    ) -> list[dict]:
        """Build Duped file-tree rows from staged Same pairs."""
        out: list[dict] = []
        seen: set[bytes] = set()
        for a, b in pairs:
            # Prefer the digest that has an alias under focus_key as local.
            local, peer = a, b
            try:
                aliases_a = self.store.list_name_aliases(a) if self.store else []
            except Exception:
                aliases_a = []
            try:
                aliases_b = self.store.list_name_aliases(b) if self.store else []
            except Exception:
                aliases_b = []
            a_here = any((x.get('gallery_key') or '') == focus_key for x in aliases_a)
            b_here = any((x.get('gallery_key') or '') == focus_key for x in aliases_b)
            if b_here and not a_here:
                local, peer = b, a
                aliases_a, aliases_b = aliases_b, aliases_a
            if local in seen:
                continue
            seen.add(local)
            merged_aliases = []
            for src, sha in ((aliases_a, local), (aliases_b, peer)):
                for x in src:
                    merged_aliases.append(
                        {
                            'name': x.get('name'),
                            'bare_name': x.get('bare_name'),
                            'gallery_key': x.get('gallery_key') or '',
                            'sample_path': x.get('sample_path'),
                            'sha1': sha,
                        }
                    )
            sample = None
            home_gk = ''
            for x in aliases_a:
                if x.get('sample_path'):
                    sample = x.get('sample_path')
                    break
            for x in aliases_a:
                if (x.get('gallery_key') or '') == focus_key and x.get('sample_path'):
                    sample = x.get('sample_path')
                    home_gk = focus_key
                    break
            if self.store:
                try:
                    hit = self.store.lookup_sha1(local)
                except Exception:
                    hit = None
                if hit:
                    sample = sample or hit.get('sample_path')
                    home_gk = home_gk or (hit.get('gallery_key') or '')
            out.append(
                {
                    'sha1': local,
                    'sha1_hex': local.hex(),
                    'sample_path': sample,
                    'home_gallery_key': home_gk or focus_key,
                    'byte_len': None,
                    'seen_count': 0,
                    'home_decided': False,
                    'peer_sha1': peer if peer != local else None,
                    'aliases': merged_aliases,
                    'match_kind': 'exact' if local == peer else 'match_group',
                }
            )
        out.sort(key=lambda x: x['sha1_hex'])
        return out

    def _duped_load_file_items(self, files: list[dict]) -> None:
        """Replace the file tree with ``files`` (UI thread)."""
        self._duped_hide_preview()
        self._duped_seq_cache.clear()
        try:
            self.duped_file_tree.delete(*self.duped_file_tree.get_children())
        except tk.TclError:
            pass
        self._duped_files.clear()
        self._duped_file_list = list(files)
        self._duped_file_pop_gen += 1
        gen = self._duped_file_pop_gen
        key = self._duped_focus_key or ''
        near = self._duped_is_near()
        undecided = bool(self.duped_undecided_var.get())
        self._duped_populate_files_chunk(gen, key, near, undecided, files, 0)

    def _duped_session_show(self) -> None:
        sess = self._session
        w = self._duped_cw_widgets
        if not sess or not w or self._duped_compare_win is None:
            return
        focus_key = sess['focus_key']
        peer_key = sess['peer_key']
        left_seq, right_seq = self._duped_session_walks()
        li, ri = sess['left_idx'], sess['right_idx']
        left_slot = left_seq[li] if 0 <= li < len(left_seq) else {}
        right_slot = right_seq[ri] if 0 <= ri < len(right_seq) else {}
        left_name = left_slot.get('name') or '?'
        right_name = right_slot.get('name') or '?'
        left_path = left_slot.get('path')
        right_path = right_slot.get('path')
        sha_l, sha_r = left_slot.get('sha1'), right_slot.get('sha1')
        key = self._session_pair_key(sha_l, sha_r)
        if key is None and sha_l and sha_r and sha_l == sha_r:
            key = (sha_l, sha_r)
        badge = ''
        if key and key in sess['staged_same']:
            badge = 'STAGED: Same'
        elif key and key in sess['staged_fp']:
            badge = 'STAGED: False positive'
        elif sha_l and sha_r and sha_l == sha_r:
            badge = 'exact SHA (already identical)'
        off_l = li - sess['anchor_left']
        off_r = ri - sess['anchor_right']
        n_same = len(sess['staged_same'])
        n_fp = len(sess['staged_fp'])
        if sess.get('pattern_mode'):
            pat = (
                f'pattern {sess.get("left_family") or "?"} ↔ '
                f'{sess.get("right_family") or "?"}  ·  '
            )
        else:
            pat = 'full gallery  ·  '
        try:
            w['title'].configure(
                text=(
                    f'{pat}'
                    f'{focus_key} [{li + 1}/{max(1, len(left_seq))}]  ↔  '
                    f'{peer_key} [{ri + 1}/{max(1, len(right_seq))}]  ·  '
                    f'offset {off_l:+d}/{off_r:+d}'
                )
            )
            w['meta'].configure(
                text=f'This: {left_path or "(missing)"}\nPeer: {right_path or "(missing)"}'
            )
            w['status'].configure(
                text=f'{badge}   ·   staged Same {n_same} · FP {n_fp}'
                if badge
                else f'staged Same {n_same} · FP {n_fp}'
            )
            w['left_name'].configure(text=f'{focus_key}\n{left_name}')
            w['right_name'].configure(text=f'{peer_key}\n{right_name}')
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
        try:
            self._duped_compare_win.title(
                f'Duped session — Same {n_same} · FP {n_fp}'
            )
        except Exception:
            pass

    def _duped_session_staged_shas(self) -> set[bytes]:
        sess = self._session
        out: set[bytes] = set()
        if not sess:
            return out
        for a, b in sess.get('staged_same') or []:
            out.add(a)
            out.add(b)
        return out

    def _duped_session_open_more(self) -> None:
        sess = self._session
        if not sess:
            return
        self._duped_close_more_win()
        win = tk.Toplevel(self._duped_compare_win or self)
        win.title('More — pick This + Peer')
        win.configure(background='#1e1e1e')
        try:
            win.geometry('980x640')
            win.minsize(640, 400)
        except Exception:
            pass
        win.protocol('WM_DELETE_WINDOW', self._duped_close_more_win)
        hint = tk.Label(
            win,
            text=(
                'Click one This (blue) and one Peer (orange). Staged Same hidden. '
                'Outside pattern widens walk to full gallery.'
            ),
            fg='#ccc',
            bg='#1e1e1e',
            font=('Segoe UI', 9),
        )
        hint.pack(anchor='w', padx=8, pady=6)
        body = tk.Frame(win, bg='#1e1e1e')
        body.pack(fill='both', expand=True, padx=6, pady=4)
        left_fr = tk.Frame(body, bg='#1e1e1e')
        right_fr = tk.Frame(body, bg='#1e1e1e')
        left_fr.pack(side='left', fill='both', expand=True, padx=(0, 4))
        right_fr.pack(side='left', fill='both', expand=True, padx=(4, 0))
        self._duped_more_win = win
        self._duped_more_photos = []
        self._duped_cw_widgets['more_left'] = left_fr
        self._duped_cw_widgets['more_right'] = right_fr
        self._duped_cw_widgets['more_hint'] = hint
        self._duped_session_refresh_more()

    def _duped_session_refresh_more(self) -> None:
        sess = self._session
        left_fr = self._duped_cw_widgets.get('more_left')
        right_fr = self._duped_cw_widgets.get('more_right')
        if not sess or left_fr is None or right_fr is None:
            return
        for fr in (left_fr, right_fr):
            for child in list(fr.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass
        excluded = self._duped_session_staged_shas()
        self._duped_more_photos = []
        self._duped_fill_more_side(
            left_fr,
            sess['focus_key'],
            side='left',
            border=DUPED_COLOR_LEFT,
            excluded=excluded,
        )
        self._duped_fill_more_side(
            right_fr,
            sess['peer_key'],
            side='right',
            border=DUPED_COLOR_RIGHT,
            excluded=excluded,
        )

    def _duped_fill_more_side(
        self,
        parent,
        gallery_key: str,
        *,
        side: str,
        border: str,
        excluded: set[bytes],
    ) -> None:
        tk.Label(
            parent,
            text=f'{"This" if side == "left" else "Peer"}  {gallery_key}',
            fg=border,
            bg='#1e1e1e',
            font=('Segoe UI', 9, 'bold'),
        ).pack(anchor='w')
        canvas = tk.Canvas(parent, bg='#151515', highlightthickness=0)
        scroll = ttk.Scrollbar(parent, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        inner = tk.Frame(canvas, bg='#151515')
        win_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def _on_canvas(event):
            canvas.itemconfigure(win_id, width=event.width)

        inner.bind('<Configure>', _on_inner)
        canvas.bind('<Configure>', _on_canvas)
        canvas.bind(
            '<MouseWheel>',
            lambda e: canvas.yview_scroll(int(-e.delta / 120), 'units'),
        )

        seq = self._duped_sequence(gallery_key)
        box = (DUPED_MORE_THUMB, DUPED_MORE_THUMB)
        cols = 3
        row_fr = None
        shown = 0
        for idx, slot in enumerate(seq):
            sha = slot.get('sha1')
            if sha and sha in excluded:
                continue
            path = slot.get('path')
            if path is None:
                continue
            if shown % cols == 0:
                row_fr = tk.Frame(inner, bg='#151515')
                row_fr.pack(fill='x', pady=2)
            cell = tk.Frame(
                row_fr,
                bg='#222',
                highlightbackground=border,
                highlightthickness=1,
            )
            cell.pack(side='left', padx=3, pady=2)
            lbl = tk.Label(cell, text='…', fg='#888', bg='#222')
            lbl.pack(padx=2, pady=2)
            name = (slot.get('name') or '')[:28]
            tk.Label(
                cell, text=name, fg='#bbb', bg='#222', font=('Segoe UI', 7)
            ).pack()
            self._duped_queue_thumb(lbl, path, box, fast=True)
            for w in (cell, lbl):
                w.bind(
                    '<Button-1>',
                    lambda _e, s=side, i=idx: self._duped_more_pick(s, i),
                )
            shown += 1

    def _duped_more_pick(self, side: str, index: int) -> None:
        sess = self._session
        if not sess:
            return
        if side == 'left':
            sess['more_pick_left'] = int(index)
        else:
            sess['more_pick_right'] = int(index)
        left_i = sess.get('more_pick_left')
        right_i = sess.get('more_pick_right')
        hint = self._duped_cw_widgets.get('more_hint')
        if left_i is None or right_i is None:
            if hint is not None:
                missing = 'Peer' if left_i is not None else 'This'
                try:
                    hint.configure(text=f'Selected {side}; now click {missing}.')
                except Exception:
                    pass
            return
        # More lists full gallery order; map into the current walk slices.
        focus_key = sess['focus_key']
        peer_key = sess['peer_key']
        full_l = self._duped_sequence(focus_key)
        full_r = self._duped_sequence(peer_key)
        if not (0 <= left_i < len(full_l) and 0 <= right_i < len(full_r)):
            return
        slot_l = full_l[left_i]
        slot_r = full_r[right_i]
        walk_l, walk_r = self._duped_session_walks()

        def _find(walk: list[dict], slot: dict) -> int | None:
            for i, s in enumerate(walk):
                if s.get('name') == slot.get('name') and (
                    s.get('sha1') == slot.get('sha1') or not slot.get('sha1')
                ):
                    return i
            name = slot.get('name')
            for i, s in enumerate(walk):
                if s.get('name') == name:
                    return i
            return None

        wl = _find(walk_l, slot_l)
        wr = _find(walk_r, slot_r)
        if wl is None or wr is None:
            # Outside pattern slice — widen to full gallery for the rest of session.
            sess['walk_left'] = list(full_l)
            sess['walk_right'] = list(full_r)
            sess['pattern_mode'] = False
            sess['left_family'] = None
            sess['right_family'] = None
            wl = left_i
            wr = right_i
            sess['anchor_left'] = wl
            sess['anchor_right'] = wr
        sess['left_idx'] = int(wl)
        sess['right_idx'] = int(wr)
        sess['more_pick_left'] = None
        sess['more_pick_right'] = None
        self._duped_close_more_win()
        self._duped_session_show()

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
        self._on_duped_gallery_select()


    def duped_refresh(self):
        if not self.store:
            messagebox.showwarning('Duped', 'Database not ready.')
            return
        if self._duped_busy:
            messagebox.showinfo('Duped', 'Busy — wait for the current job.')
            return
        near = self._duped_is_near()
        undecided = bool(self.duped_undecided_var.get())
        self._duped_busy = True
        self.duped_status.set('Refreshing galleries…')
        prev_sel = list(self.duped_gallery_tree.selection())

        def work():
            try:
                if near:
                    rows = self.store.list_near_dupe_galleries(limit=500)
                else:
                    rows = self.store.list_dupe_galleries(
                        limit=500,
                        undecided_only=undecided,
                    )
                err = None
            except Exception as e:
                rows = []
                err = e
            self._ui_schedule(
                lambda: self._duped_refresh_apply(rows, err, near, undecided, prev_sel)
            )

        threading.Thread(target=work, daemon=True, name='duped-refresh').start()

    def _duped_refresh_apply(
        self,
        rows: list[dict],
        err: Exception | None,
        near: bool,
        undecided: bool,
        prev_sel: list[str],
    ):
        self._duped_busy = False
        if not self._lifecycle_alive:
            return
        if err is not None:
            messagebox.showerror('Duped', f'Query failed:\n{err}')
            self.duped_status.set('Refresh failed')
            return
        self._duped_rows.clear()
        self._duped_seq_cache.clear()
        self._duped_clear_compare()
        self.duped_gallery_tree.delete(*self.duped_gallery_tree.get_children())
        self.duped_file_tree.delete(*self.duped_file_tree.get_children())
        self._duped_files.clear()
        self._duped_file_list = []
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
            if near or shared is None or not undecided:
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
            mode = 'undecided' if undecided else 'all'
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
        self._duped_file_list = list(files)
        self._duped_file_pop_gen += 1
        gen = self._duped_file_pop_gen
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
                f'{key}: {len(files)} near match(es) — double-click compare session'
            )
        else:
            mode = 'undecided' if undecided else 'all'
            self.duped_status.set(
                f'{key}: {len(files)} shared ({mode}) — double-click compare session'
            )
            self._duped_scan_set_siblings(gen, key)

    def _duped_scan_set_siblings(self, gen: int, key: str):
        """Background: find hole-fill orphans for dedicated-set home."""
        if not self.store or self._duped_is_near():
            return
        store = self.store

        def work():
            if (
                gen != self._duped_file_pop_gen
                or not self._lifecycle_alive
                or key != self._duped_focus_key
            ):
                return
            try:
                seed = store.list_shared_files_for_gallery(
                    key, undecided_only=False
                )
            except Exception as e:
                log.exception('set-sibling seed failed for %s', key)
                self.ui_log(f'Set-sibling scan failed: {e}')
                return

            def resolve_path(sample_path, gallery_key, name):
                real = resolve_real_file(sample_path)
                if real is not None:
                    return real
                try:
                    meta = store.resolve_gallery_meta(gallery_key)
                except Exception:
                    meta = None
                out = (meta or {}).get('out_dir')
                if out and name:
                    return resolve_real_file(Path(out) / name)
                return None

            try:
                siblings = suggest_set_siblings(
                    seed,
                    key,
                    list_aliases_for_gallery=store.list_aliases_for_gallery,
                    gallery_has_sha=store.gallery_has_sha,
                    resolve_path=resolve_path,
                )
            except Exception as e:
                log.exception('set-sibling suggest failed for %s', key)
                self.ui_log(f'Set-sibling suggest failed: {e}')
                return

            def done():
                if (
                    gen != self._duped_file_pop_gen
                    or not self._lifecycle_alive
                    or key != self._duped_focus_key
                ):
                    return
                self._duped_append_siblings(key, siblings)

            self._ui_schedule(done)

        threading.Thread(
            target=work, name='duped-set-sib', daemon=True
        ).start()

    def _duped_append_siblings(self, key: str, siblings: list[dict]):
        if not siblings:
            return
        existing = set(self._duped_files.keys())
        added = 0
        for item in siblings:
            digest = item.get('sha1')
            if not digest:
                continue
            iid = digest.hex() if isinstance(digest, bytes) else str(digest)
            if iid in existing:
                continue
            local_name = item.get('_local_name') or item.get('suggested_home_name')
            peer_key = item.get('_peer_key') or ''
            peer_name = item.get('_peer_name') or item.get('source_name') or ''
            peer_label = ''
            if peer_key:
                peer_label = (
                    f'{peer_key}: {peer_name}' if peer_name else peer_key
                )
            home_col = item.get('preferred_home_key') or ''
            reason = item.get('sibling_reason') or 'set sibling?'
            this_path = item.get('_this_path') or f'({reason})'
            peer_path = item.get('_peer_path') or ''
            self._duped_files[iid] = item
            existing.add(iid)
            try:
                self.duped_file_tree.insert(
                    '',
                    0,
                    iid=iid,
                    tags=('set_sibling',),
                    values=(
                        local_name or iid[:12],
                        this_path,
                        peer_label,
                        peer_path,
                        home_col,
                    ),
                )
                added += 1
            except tk.TclError:
                pass
        if added:
            cur = self.duped_status.get() if hasattr(self, 'duped_status') else ''
            self.duped_status.set(
                f'{cur} · {added} set-sibling hint(s)' if cur else
                f'{key}: {added} set-sibling hint(s)'
            )
            log_feed(
                log,
                logging.INFO,
                'Duped %s: %s set-sibling hint(s)',
                key,
                added,
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
        focus_home = gsel[0]

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
        # Set-sibling rows prefer the dedicated-set gallery as home.
        by_home: dict[str, list[dict]] = {}
        for item in items:
            hk = (item.get('preferred_home_key') or focus_home).strip()
            by_home.setdefault(hk, []).append(item)

        home_dirs: dict[str, Path] = {}
        for hk in by_home:
            home_row = self._duped_rows.get(hk) or {}
            home_dir = home_row.get('out_dir')
            if not home_dir:
                gal = None
                try:
                    gal = self.store.resolve_gallery_meta(hk)
                except Exception:
                    pass
                home_dir = (gal or {}).get('out_dir')
            if not home_dir:
                messagebox.showwarning(
                    'Duped',
                    f'No out_dir for gallery {hk}. Complete/import it first.',
                )
                return
            home_dirs[hk] = Path(home_dir)

        create_links = bool(self.duped_links_var.get())
        n = len(items)
        scope_label = 'all listed' if scope == 'all' else 'selected'
        verb = 'move + link peers' if create_links else 'move without peer links'
        sib_n = sum(1 for it in items if it.get('kind') == 'set_sibling')
        home_bits = ', '.join(
            f'{hk} ({len(v)})' for hk, v in by_home.items()
        )
        dest_lines = '\n'.join(str(p) for p in home_dirs.values())
        extra = ''
        if sib_n:
            extra = (
                f'\n{sib_n} set-sibling hint(s) → dedicated set home '
                f'(rename to set pattern).'
            )
        if not messagebox.askyesno(
            'Duped',
            f'Move to home — {n} {scope_label} file(s)\n'
            f'({verb}).\n'
            f'Homes: {home_bits}{extra}\n\n'
            f'Destination(s):\n{dest_lines}',
        ):
            return

        self._duped_busy = True
        self._duped_stop.clear()
        self.duped_status.set(f'Applying home…')

        def work():
            ok = 0
            fail = 0
            try:
                for hk, group in by_home.items():
                    home_dir = home_dirs[hk]
                    for item in group:
                        if self._duped_stop.is_set() or not self._lifecycle_alive:
                            break
                        try:
                            self._duped_appoint_one(
                                item,
                                home_key=hk,
                                home_dir=home_dir,
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
                        'Duped home apply — ok=%s fail=%s links=%s scope=%s',
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
        aliases = list(item.get('aliases') or [])
        digest = item.get('sha1')
        # Expand match-group aliases (same as Move).
        if self.store and digest:
            try:
                equiv = set(self.store.equivalent_shas(digest))
            except Exception:
                equiv = {digest} if digest else set()
            peer_sha = item.get('peer_sha1')
            if peer_sha:
                equiv.add(peer_sha)
            seen = {
                (a.get('gallery_key'), a.get('name'), a.get('sha1')) for a in aliases
            }
            for eq in equiv:
                if eq == digest:
                    continue
                try:
                    extra = self.store.list_name_aliases(eq)
                except Exception:
                    extra = []
                for a in extra:
                    row = {
                        'name': a.get('name'),
                        'bare_name': a.get('bare_name'),
                        'gallery_key': a.get('gallery_key') or '',
                        'sample_path': a.get('sample_path'),
                        'sha1': eq,
                    }
                    sig = (row['gallery_key'], row['name'], eq)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    aliases.append(row)

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
        focus = self._duped_focus_key or home_key
        removed = 0
        for a in aliases:
            gkey = (a.get('gallery_key') or '').strip()
            if not gkey or (focus and gkey == focus) or (home_key and gkey == home_key):
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
            a_sha = a.get('sha1')
            status = strip_peer_presence(peer_path, real_keep=real)
            if status in ('link', 'dup'):
                removed += 1
                continue
            # Match-group peer may differ in size — still strip when confirmed.
            if a_sha and digest and a_sha != digest:
                st = remove_peer_any(peer_path, real_keep=real)
                if st in ('link', 'file'):
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
        aliases = list(item.get('aliases') or [])
        # Expand match-group members so peer galleries of equivalent SHAs are handled.
        equiv: set[bytes] = {digest}
        if self.store:
            try:
                equiv = set(self.store.equivalent_shas(digest))
            except Exception:
                equiv = {digest}
        peer_sha = item.get('peer_sha1')
        if peer_sha:
            equiv.add(peer_sha)
        seen_alias: set[tuple] = {
            (a.get('gallery_key'), a.get('name'), a.get('sha1')) for a in aliases
        }
        for eq in equiv:
            if eq == digest or not self.store:
                continue
            try:
                extra = self.store.list_name_aliases(eq)
            except Exception:
                extra = []
            for a in extra:
                row = {
                    'name': a.get('name'),
                    'bare_name': a.get('bare_name'),
                    'gallery_key': a.get('gallery_key') or '',
                    'sample_path': a.get('sample_path'),
                    'sha1': eq,
                }
                sig = (row['gallery_key'], row['name'], eq)
                if sig in seen_alias:
                    continue
                seen_alias.add(sig)
                aliases.append(row)

        # Preferred name in home gallery.
        home_name = None
        suggested = (item.get('suggested_home_name') or '').strip()
        if suggested:
            home_name = suggested
        if not home_name:
            for a in aliases:
                if a.get('gallery_key') == home_key:
                    home_name = a.get('name') or a.get('bare_name')
                    if home_name:
                        break
        if not home_name:
            home_name = Path(item.get('sample_path') or 'file.bin').name

        # Prefer a real file already under the home folder.
        real = None
        for a in aliases:
            if a.get('gallery_key') != home_key:
                continue
            real = resolve_real_file(a.get('sample_path'))
            if real is None:
                n = a.get('name') or a.get('bare_name')
                if n:
                    real = resolve_real_file(home_dir / n)
            if real is not None:
                break
        if real is None:
            real = resolve_real_file(item.get('sample_path'))
        if real is None:
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

        # Point every match-group member at the home file.
        for eq in equiv:
            try:
                self.store.set_fingerprint_home(
                    eq,
                    sample_path=str(moved),
                    gallery_key=home_key,
                )
            except Exception as e:
                if eq == digest:
                    raise
                self.ui_log(f'  home meta for {eq.hex()[:10]}: {e}')

        # Ensure home gallery has a name alias (set-sibling rename path).
        try:
            self.store.record_name_alias(
                digest,
                name=str(home_name),
                bare_name=Path(str(home_name)).stem,
                gallery_key=home_key,
                sample_path=str(moved),
            )
        except Exception as e:
            self.ui_log(f'  home alias {home_name}: {e}')

        # Peer sites: symlink or remove presence (aliases stay in DB).
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
            a_sha = a.get('sha1')
            if create_links:
                status = ensure_symlink(peer_path, moved)
                if status == 'exists_real' and not same_path(peer_path, moved):
                    try:
                        peer_path.unlink()
                        ensure_symlink(peer_path, moved)
                    except OSError as e:
                        self.ui_log(f'  peer replace failed {peer_path.name}: {e}')
            else:
                # Exact dup / symlink, or match-group peer with different bytes.
                if remove_path_if_link_or_dup(peer_path, real_keep=moved):
                    continue
                if a_sha and a_sha != digest:
                    st = remove_peer_any(peer_path, real_keep=moved)
                    if st == 'skip':
                        self.ui_log(f'  peer keep/skip {peer_path}')

