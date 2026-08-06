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
    resolve_real_file,
    same_entry,
    same_path,
    strip_peer_presence,
)
from image_dhash import DEFAULT_MAX_HAMMING
from local_import import list_images, nat_key
from logger import get_logger, log_feed

log = get_logger("duped_tab")

DUPED_COLOR_LEFT = "#2B6CB0"
DUPED_COLOR_RIGHT = "#C05621"
DUPED_COLOR_FOCUS = "#ECC94B"
DUPED_NEIGHBOR_RADIUS = 3
DUPED_THUMB_SIZE = 86
DUPED_COMPARE_SIZE = 220
DUPED_BOARD_THUMB_W = 56
DUPED_BOARD_THUMB_H = 80
DUPED_BOARD_BATCH = 2
DUPED_COMPARE_WIN_W = 520
DUPED_COMPARE_WIN_H = 700
DUPED_MANUAL_HAMMING = 255
DUPED_THUMB_APPLY_PER_TICK = 12
DUPED_THUMB_READY_MAX = 240
DUPED_TREE_CHUNK = 60


class DupedTab(ttk.Frame):
    """Exact/Near dupe review: gallery list, match board, large compare window."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host

        self._duped_rows: dict[str, dict] = {}
        self._duped_files: dict[str, dict] = {}
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
                'Exact = SHA-1. Near = dHash. Match board = index (\u00b13). '
                'Double-click row/card \u2192 large compare. Right-click \u2192 Explorer.'
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

        body = ttk.Panedwindow(self, orient='vertical')
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

