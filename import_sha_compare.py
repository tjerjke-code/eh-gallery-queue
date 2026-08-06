"""Import SHA match compare picker — pick among f_shash gallery candidates."""

from __future__ import annotations

import io
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from eh_title_search import pick_sample_files

BG = "#1e1e1e"
CARD_BG = "#111"
CARD_SEL = "#2B6CB0"
FG = "#eee"
FG_DIM = "#aaa"
FG_MUTED = "#666"
COVER_BOX = (160, 220)
THUMB_BOX = (72, 72)
LOCAL_THUMB = (88, 88)

_active_win: tk.Toplevel | None = None
_active_photos: list = []


def close_active_sha_compare() -> None:
    """Destroy an open compare dialog (reload / stop search)."""
    global _active_win, _active_photos
    win = _active_win
    _active_win = None
    _active_photos = []
    if win is not None:
        try:
            win.destroy()
        except tk.TclError:
            pass


def ask_sha_match(
    parent: tk.Misc,
    folder_name: str,
    folder: Path | str | None,
    candidates: list[dict],
) -> dict | None:
    """Modal compare: return chosen candidate, or None if skipped / closed."""
    global _active_win, _active_photos
    if not candidates:
        return None
    close_active_sha_compare()

    folder_path = Path(folder) if folder else None
    result: dict = {"hit": None}

    win = tk.Toplevel(parent)
    win.title("Import SHA match")
    win.configure(background=BG)
    try:
        win.geometry("1100x720")
        win.minsize(780, 520)
    except Exception:
        pass
    win.transient(parent)
    try:
        win.grab_set()
    except tk.TclError:
        pass

    photos: list = []
    _active_photos = photos
    _active_win = win

    selected: dict = {"idx": 0 if len(candidates) == 1 else None}
    card_frames: list[tk.Frame] = []
    confirm_btn: ttk.Button | None = None

    def finish(hit: dict | None) -> None:
        global _active_win, _active_photos
        result["hit"] = hit
        if _active_win is win:
            _active_win = None
            _active_photos = []
        try:
            win.grab_release()
        except tk.TclError:
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    def photo_from_path(path: Path, box: tuple[int, int]):
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return None
        try:
            with Image.open(path) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                else:
                    im = im.copy()
                im.thumbnail(box, Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", box, (40, 40, 40))
                canvas.paste(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
                photo = ImageTk.PhotoImage(canvas)
                photos.append(photo)
                return photo
        except Exception:
            return None

    def photo_from_bytes(data: bytes | None, box: tuple[int, int]):
        if not data:
            return None
        try:
            from PIL import Image, ImageTk
        except ImportError:
            return None
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.mode != "RGB":
                    im = im.convert("RGB")
                else:
                    im = im.copy()
                im.thumbnail(box, Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", box, (40, 40, 40))
                canvas.paste(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
                photo = ImageTk.PhotoImage(canvas)
                photos.append(photo)
                return photo
        except Exception:
            return None

    def refresh_selection() -> None:
        idx = selected["idx"]
        for i, fr in enumerate(card_frames):
            color = CARD_SEL if i == idx else "#333"
            try:
                fr.configure(highlightbackground=color, highlightthickness=3)
            except tk.TclError:
                pass
        if confirm_btn is not None:
            try:
                confirm_btn.configure(state="normal" if idx is not None else "disabled")
            except tk.TclError:
                pass

    def select_idx(i: int) -> None:
        if 0 <= i < len(candidates):
            selected["idx"] = i
            refresh_selection()

    def confirm() -> None:
        idx = selected["idx"]
        if idx is None or not (0 <= idx < len(candidates)):
            return
        finish(candidates[idx])

    def open_eh(url: str) -> None:
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    root = tk.Frame(win, bg=BG, padx=10, pady=8)
    root.pack(fill="both", expand=True)

    tk.Label(
        root,
        text="No title match — pick an EH gallery from SHA samples",
        fg=FG,
        bg=BG,
        font=("Segoe UI", 11, "bold"),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        root,
        text=folder_name or "(folder)",
        fg=FG_DIM,
        bg=BG,
        font=("Segoe UI", 9),
        wraplength=1040,
        justify="left",
        anchor="w",
    ).pack(fill="x", pady=(2, 6))

    # Local sample strip
    local_fr = tk.Frame(root, bg=CARD_BG, padx=8, pady=6)
    local_fr.pack(fill="x", pady=(0, 8))
    local_count = None
    for c in candidates:
        if c.get("local_count") is not None:
            local_count = c.get("local_count")
            break
    sample_paths: list[Path] = []
    if folder_path and folder_path.is_dir():
        names: list[str] = []
        for c in candidates:
            for n in c.get("sample_files") or []:
                if n and n not in names:
                    names.append(n)
        if names:
            for n in names:
                p = folder_path / n
                if p.is_file():
                    sample_paths.append(p)
        if not sample_paths:
            sample_paths = pick_sample_files(folder_path, 3)

    header = f"Local folder — {local_count if local_count is not None else '?'} file(s)"
    if sample_paths:
        header += " · SHA samples:"
    tk.Label(
        local_fr, text=header, fg=FG, bg=CARD_BG, font=("Segoe UI", 9, "bold"), anchor="w"
    ).pack(fill="x")
    strip = tk.Frame(local_fr, bg=CARD_BG)
    strip.pack(fill="x", pady=(4, 0))
    if sample_paths:
        for p in sample_paths:
            cell = tk.Frame(strip, bg=CARD_BG)
            cell.pack(side="left", padx=4)
            ph = photo_from_path(p, LOCAL_THUMB)
            if ph is not None:
                tk.Label(cell, image=ph, bg=CARD_BG).pack()
            else:
                tk.Label(cell, text="?", fg=FG_MUTED, bg=CARD_BG, width=8, height=4).pack()
            tk.Label(
                cell,
                text=p.name[:28],
                fg=FG_DIM,
                bg=CARD_BG,
                font=("Segoe UI", 7),
                wraplength=90,
            ).pack()
    else:
        tk.Label(
            strip, text="(no local sample previews)", fg=FG_MUTED, bg=CARD_BG
        ).pack(anchor="w")

    # Scrollable candidate cards
    canvas_fr = tk.Frame(root, bg=BG)
    canvas_fr.pack(fill="both", expand=True)
    canvas = tk.Canvas(canvas_fr, bg=BG, highlightthickness=0)
    xscroll = ttk.Scrollbar(canvas_fr, orient="horizontal", command=canvas.xview)
    canvas.configure(xscrollcommand=xscroll.set)
    xscroll.pack(side="bottom", fill="x")
    canvas.pack(side="top", fill="both", expand=True)
    cards_host = tk.Frame(canvas, bg=BG)
    cards_win = canvas.create_window((0, 0), window=cards_host, anchor="nw")

    def _on_cards_configure(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(event):
        try:
            canvas.itemconfigure(cards_win, height=event.height)
        except tk.TclError:
            pass

    cards_host.bind("<Configure>", _on_cards_configure)
    canvas.bind("<Configure>", _on_canvas_configure)

    for i, c in enumerate(candidates):
        card = tk.Frame(
            cards_host,
            bg=CARD_BG,
            highlightbackground="#333",
            highlightthickness=3,
            padx=8,
            pady=8,
        )
        card.pack(side="left", fill="y", padx=6, pady=4, anchor="n")
        card_frames.append(card)

        def bind_select(widget, idx=i):
            widget.bind("<Button-1>", lambda _e, j=idx: select_idx(j))
            widget.bind("<Double-Button-1>", lambda _e, j=idx: (select_idx(j), confirm()))

        bind_select(card)

        key = c.get("gallery_key") or "?"
        votes = c.get("votes")
        of = c.get("vote_of")
        vote_s = f"{votes}/{of}" if votes is not None and of is not None else "?"
        title = (c.get("title") or "(no title)").strip()
        title_jpn = (c.get("title_jpn") or "").strip()
        eh_total = c.get("image_total")
        loc = c.get("local_count", local_count)
        count_note = ""
        if eh_total is not None and loc is not None:
            try:
                eh_i, loc_i = int(eh_total), int(loc)
                if eh_i == loc_i:
                    count_note = f"pages {eh_i} = local"
                else:
                    count_note = f"pages {eh_i} vs local {loc_i}"
            except (TypeError, ValueError):
                count_note = f"pages {eh_total}"
        elif eh_total is not None:
            count_note = f"pages {eh_total}"

        head = tk.Label(
            card,
            text=f"#{i + 1}  {key}  (SHA {vote_s})",
            fg=FG,
            bg=CARD_BG,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            wraplength=250,
            justify="left",
        )
        head.pack(fill="x")
        bind_select(head)

        tlab = tk.Label(
            card,
            text=title,
            fg=FG_DIM,
            bg=CARD_BG,
            font=("Segoe UI", 8),
            wraplength=250,
            justify="left",
            anchor="w",
        )
        tlab.pack(fill="x", pady=(2, 0))
        bind_select(tlab)
        if title_jpn:
            jlab = tk.Label(
                card,
                text=title_jpn,
                fg=FG_MUTED,
                bg=CARD_BG,
                font=("Segoe UI", 8),
                wraplength=250,
                justify="left",
                anchor="w",
            )
            jlab.pack(fill="x")
            bind_select(jlab)

        meta_bits = []
        if c.get("category"):
            meta_bits.append(str(c["category"]))
        if count_note:
            meta_bits.append(count_note)
        if c.get("posted"):
            meta_bits.append(str(c["posted"]))
        langs = c.get("languages") or []
        if langs:
            meta_bits.append(", ".join(langs[:3]))
        if c.get("file_size"):
            meta_bits.append(str(c["file_size"]))
        if c.get("enrich_error"):
            meta_bits.append(f"enrich err: {c['enrich_error'][:40]}")
        meta_txt = " · ".join(meta_bits) if meta_bits else "(no page meta)"
        mlab = tk.Label(
            card,
            text=meta_txt,
            fg=FG_MUTED,
            bg=CARD_BG,
            font=("Segoe UI", 7),
            wraplength=250,
            justify="left",
            anchor="w",
        )
        mlab.pack(fill="x", pady=(2, 4))
        bind_select(mlab)

        cover_ph = photo_from_bytes(c.get("cover_bytes"), COVER_BOX)
        if cover_ph is not None:
            clab = tk.Label(card, image=cover_ph, bg=CARD_BG)
            clab.pack(pady=4)
            bind_select(clab)
        else:
            tk.Label(
                card, text="(no cover)", fg=FG_MUTED, bg=CARD_BG, height=8
            ).pack(pady=4)

        thumb_row = tk.Frame(card, bg=CARD_BG)
        thumb_row.pack(fill="x")
        tbytes = c.get("page_thumb_bytes") or []
        shown = 0
        for tb in tbytes:
            ph = photo_from_bytes(tb, THUMB_BOX)
            if ph is None:
                continue
            tl = tk.Label(thumb_row, image=ph, bg=CARD_BG)
            tl.pack(side="left", padx=2)
            bind_select(tl)
            shown += 1
            if shown >= 4:
                break
        if shown == 0:
            tk.Label(
                thumb_row, text="(no page thumbs)", fg=FG_MUTED, bg=CARD_BG, font=("Segoe UI", 7)
            ).pack(anchor="w")

        samples = c.get("sample_files") or []
        if samples:
            slab = tk.Label(
                card,
                text="Matched samples: " + ", ".join(str(s)[:24] for s in samples[:5]),
                fg=FG_DIM,
                bg=CARD_BG,
                font=("Segoe UI", 7),
                wraplength=250,
                justify="left",
                anchor="w",
            )
            slab.pack(fill="x", pady=(6, 0))
            bind_select(slab)

        url = c.get("url") or ""
        link = ttk.Button(
            card, text="Open EH", width=12, command=lambda u=url: open_eh(u)
        )
        link.pack(anchor="w", pady=(8, 0))

        pick = ttk.Button(
            card, text="Select", width=12, command=lambda j=i: select_idx(j)
        )
        pick.pack(anchor="w", pady=(4, 0))

    # Footer
    foot = tk.Frame(root, bg=BG)
    foot.pack(fill="x", pady=(8, 0))
    confirm_btn = ttk.Button(foot, text="Confirm", width=12, command=confirm)
    confirm_btn.pack(side="left")
    ttk.Button(foot, text="None", width=10, command=lambda: finish(None)).pack(
        side="left", padx=6
    )
    tk.Label(
        foot,
        text="Click a card · Double-click to confirm · Esc = None",
        fg=FG_MUTED,
        bg=BG,
        font=("Segoe UI", 8),
    ).pack(side="right")

    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    win.bind("<Escape>", lambda _e: finish(None))
    win.bind("<Return>", lambda _e: confirm())
    refresh_selection()

    try:
        win.focus_set()
    except tk.TclError:
        pass
    parent.wait_window(win)
    close_active_sha_compare()
    return result.get("hit")
