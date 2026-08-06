"""Pixiv tab — check / fill missing pages for EH-imported Pixiv sets."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from logger import get_logger, log_feed
from pixiv import (
    PixivError,
    auth_status_text,
    ensure_pixiv_gallery,
    fetch_illust_meta,
    fetch_illust_pages,
    fill_missing,
    find_set_folder,
    gallery_folder_name,
    load_auth,
    normalize_phpsessid,
    parse_illust_id,
    persist_session_cookies,
    plan_fill,
    save_auth,
    scan_local_pages,
    session_from_store,
    verify_login,
)

log = get_logger("pixiv_tab")

DEFAULT_DIR = r"a:\trt\.Pics"


class PixivTab(ttk.Frame):
    """Fill missing Pixiv pages into the EH gallery folder that owns the set."""

    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host
        self._stop = threading.Event()
        self._busy = False
        self._last_plan: dict | None = None
        self._last_folder: Path | None = None
        self._last_gallery_key = ""
        self._last_pages: list[dict] | None = None
        self._last_meta: dict | None = None
        self._build()
        self._refresh_auth_ui()

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
        self._stop.set()

    def _build(self):
        help_row = ttk.Frame(self, padding=(8, 6))
        help_row.pack(fill="x")
        ttk.Label(
            help_row,
            text=(
                "Paste PHPSESSID once (DevTools → Application/Storage → Cookies → "
                "pixiv.net → PHPSESSID). It is stored in the EH DB and reused for "
                "every Check / Fill until it expires — then paste a fresh one."
            ),
            wraplength=780,
        ).pack(side="left", anchor="w")

        auth = ttk.Frame(self, padding=(8, 0))
        auth.pack(fill="x")
        ttk.Label(auth, text="PHPSESSID:").pack(side="left")
        self.cookie_var = tk.StringVar()
        self.cookie_entry = ttk.Entry(auth, textvariable=self.cookie_var, show="*")
        self.cookie_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(auth, text="Save", command=self._save_cookie).pack(side="left")
        ttk.Button(auth, text="Test login", command=self._test_login).pack(
            side="left", padx=4
        )
        self._show_cookie = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            auth,
            text="Show",
            variable=self._show_cookie,
            command=self._toggle_cookie_show,
        ).pack(side="left", padx=(6, 0))

        self.auth_var = tk.StringVar(value="Not logged in")
        ttk.Label(
            self,
            textvariable=self.auth_var,
            padding=(8, 2),
            foreground="#1565c0",
        ).pack(anchor="w")

        row = ttk.Frame(self, padding=(8, 8))
        row.pack(fill="x")
        ttk.Label(row, text="Illust:").pack(side="left")
        self.illust_var = tk.StringVar(value="126324413")
        self.illust_entry = ttk.Entry(row, textvariable=self.illust_var)
        self.illust_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.illust_entry.bind("<Return>", lambda _e: self.check_set())
        ttk.Button(row, text="Check", command=self.check_set).pack(side="left")
        ttk.Button(row, text="Fill missing", command=self.fill_missing).pack(
            side="left", padx=4
        )
        ttk.Button(row, text="Stop", command=self._request_stop).pack(side="left")

        meta = ttk.LabelFrame(self, text="Set", padding=8)
        meta.pack(fill="x", padx=8, pady=(0, 4))
        self.meta_var = tk.StringVar(value="Enter an illust id and press Check.")
        ttk.Label(meta, textvariable=self.meta_var, wraplength=780, justify="left").pack(
            anchor="w"
        )

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Missing pages").pack(anchor="w")
        self.missing_box = tk.Listbox(left, height=10, selectmode="extended")
        self.missing_box.pack(fill="both", expand=True)

        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(right, text="Present pages").pack(anchor="w")
        self.present_box = tk.Listbox(right, height=10)
        self.present_box.pack(fill="both", expand=True)

        ttk.Label(self, text="Log", padding=(8, 4)).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            self, height=8, state="disabled", wrap="word"
        )
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.status = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status, padding=(8, 4)).pack(fill="x")

    def _toggle_cookie_show(self):
        self.cookie_entry.configure(show="" if self._show_cookie.get() else "*")

    def _refresh_auth_ui(self, auth: dict | None = None):
        auth = auth if auth is not None else load_auth(self.store)
        cookie = auth.get("phpsessid") or ""
        try:
            if cookie:
                self.cookie_var.set(cookie)
            self.auth_var.set(auth_status_text(auth))
        except tk.TclError:
            pass

    def _ensure_cookie_saved(self) -> str:
        """Persist the entry field to DB and return the normalized PHPSESSID."""
        if not self.store:
            raise PixivError("database not connected")
        pasted = normalize_phpsessid(self.cookie_var.get())
        saved = load_auth(self.store).get("phpsessid") or ""
        if pasted and pasted != saved:
            save_auth(self.store, pasted)
            self._tab_log("PHPSESSID saved to DB")
        elif not pasted and saved:
            pasted = saved
            self.cookie_var.set(saved)
        return pasted

    def _save_cookie(self):
        if not self.store:
            messagebox.showwarning("Pixiv", "Database not connected yet.")
            return
        try:
            auth = save_auth(self.store, self.cookie_var.get())
        except Exception as e:
            messagebox.showerror("Pixiv", f"Could not save cookie:\n{e}")
            return
        self.cookie_var.set(auth.get("phpsessid") or "")
        self._refresh_auth_ui(auth)
        self._tab_log("PHPSESSID saved to app_settings (reused on later requests)")
        self.status.set("Cookie saved")

    def _test_login(self):
        if self._busy:
            messagebox.showinfo("Pixiv", "Busy — stop first.")
            return
        if not self.store:
            messagebox.showwarning("Pixiv", "Database not connected yet.")
            return
        self._stop.clear()
        self._busy = True
        self.status.set("Testing Pixiv login…")
        threading.Thread(target=self._test_login_worker, daemon=True).start()

    def _test_login_worker(self):
        try:
            cookie = self._ensure_cookie_saved()
            if not cookie:
                raise PixivError("paste PHPSESSID first")
            result = verify_login(self.store, phpsessid=cookie)
            auth = result.get("auth") or load_auth(self.store)
            name = result.get("user_name") or auth.get("user_name") or ""
            uid = result.get("user_id") or ""
            self._tab_log(f"login ok — {name or uid} ({uid})")
            self._ui_schedule(lambda a=auth: self._refresh_auth_ui(a))
            self._set_status(f"Logged in as {name or uid}")
        except Exception as e:
            log.exception("pixiv test login failed")
            self._tab_log(f"login failed: {e}")
            self._set_status(f"Login failed: {e}")
            self._ui_schedule(
                lambda: self._refresh_auth_ui()
            )
            self._ui_schedule(lambda: messagebox.showerror("Pixiv", str(e)))
        finally:
            self._busy = False

    def _request_stop(self):
        self._stop.set()
        self.status.set("Stopping…")

    def _tab_log(self, msg: str):
        def _append():
            if not self._lifecycle_alive:
                return
            try:
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
            except tk.TclError:
                pass

        self._ui_schedule(_append)
        self.ui_log(f"[pixiv] {msg}")

    def _set_status(self, text: str):
        self._ui_schedule(lambda t=text: self.status.set(t) if self._lifecycle_alive else None)

    def _clear_lists(self):
        self.missing_box.delete(0, "end")
        self.present_box.delete(0, "end")

    def _apply_plan_ui(self, plan: dict, folder: Path | None, meta: dict | None):
        if not self._lifecycle_alive:
            return
        self._clear_lists()
        for p in plan.get("missing") or []:
            self.missing_box.insert("end", f"p{p}")
        for p in plan.get("present") or []:
            self.present_box.insert("end", f"p{p}")
        title = ""
        if meta:
            title = (meta.get("title") or meta.get("illustTitle") or "").strip()
            user = (meta.get("userName") or "").strip()
            if user:
                title = f"{title} — {user}" if title else user
        folder_s = str(folder) if folder else "(not found locally — Fill will create)"
        bits = [
            f"illust {plan.get('illust_id')}",
            f"Pixiv {plan.get('remote_count')} pages",
            f"local {plan.get('local_count')}",
            f"missing {len(plan.get('missing') or [])}",
        ]
        if plan.get("complete"):
            bits.append("complete")
        line = " · ".join(bits)
        if title:
            line = f"{title}\n{line}"
        line = f"{line}\nFolder: {folder_s}"
        if not folder and meta:
            proposed = gallery_folder_name(
                meta, illust_id=str(plan.get("illust_id") or "")
            )
            line = f"{line}\nWill create: {proposed}"
        if self._last_gallery_key:
            line = f"{line}\nGallery: {self._last_gallery_key}"
        self.meta_var.set(line)

    def check_set(self):
        if self._busy:
            messagebox.showinfo("Pixiv", "Busy — stop first.")
            return
        illust = parse_illust_id(self.illust_var.get())
        if not illust:
            messagebox.showwarning("Pixiv", "Enter a Pixiv illust id or artwork URL.")
            return
        try:
            cookie = self._ensure_cookie_saved()
        except PixivError as e:
            messagebox.showwarning("Pixiv", str(e))
            return
        if not cookie:
            if not messagebox.askyesno(
                "Pixiv",
                "No PHPSESSID saved. Continue anyway?\n"
                "(R-18 / originals often need a logged-in cookie.)",
            ):
                return
        self._stop.clear()
        self._busy = True
        self.status.set(f"Checking {illust}…")
        pics = Path(self.dir_var.get().strip() or DEFAULT_DIR)
        threading.Thread(
            target=self._check_worker,
            args=(illust, pics),
            daemon=True,
        ).start()

    def _check_worker(self, illust: str, pics: Path):
        try:
            session = session_from_store(self.store)
            progress = lambda m: self._tab_log(m)
            progress(f"fetch meta {illust}")
            meta = fetch_illust_meta(session, illust)
            persist_session_cookies(self.store, session)
            if self._stop.is_set():
                return
            progress(f"fetch pages {illust}")
            pages = fetch_illust_pages(session, illust)
            persist_session_cookies(self.store, session)
            loc = find_set_folder(self.store, pics, illust)
            folder = Path(loc["folder"]) if loc else None
            gkey = (loc or {}).get("gallery_key") or ""
            local_pages = (
                scan_local_pages(folder, illust) if folder else {}
            )
            plan = plan_fill(
                illust_id=illust, pages=pages, local_pages=local_pages
            )
            self._last_plan = plan
            self._last_folder = folder
            self._last_gallery_key = gkey
            self._last_pages = pages
            self._last_meta = meta
            miss = plan.get("missing") or []
            self._tab_log(
                f"{illust}: Pixiv={plan['remote_count']} local={plan['local_count']} "
                f"missing={len(miss)}"
                + (f" {miss[:12]}" if miss else "")
            )
            if folder:
                self._tab_log(f"folder: {folder}")
            else:
                proposed = gallery_folder_name(meta, illust_id=illust)
                self._tab_log(
                    f"no local folder — Fill will create [{proposed}] under Save-to"
                )
            self._ui_schedule(
                lambda: self._apply_plan_ui(plan, folder, meta)
            )
            self._ui_schedule(lambda: self._refresh_auth_ui())
            self._set_status(
                "Complete" if plan.get("complete") else f"{len(miss)} missing"
            )
            log_feed(
                log,
                logging.INFO,
                "Pixiv check %s: %s/%s local, %s missing",
                illust,
                plan["local_count"],
                plan["remote_count"],
                len(miss),
            )
        except PixivError as e:
            self._tab_log(f"error: {e}")
            self._set_status(f"Error: {e}")
            self._ui_schedule(
                lambda: messagebox.showerror("Pixiv", str(e))
            )
        except Exception as e:
            log.exception("pixiv check failed")
            self._tab_log(f"error: {e}")
            self._set_status(f"Error: {e}")
            self._ui_schedule(
                lambda: messagebox.showerror("Pixiv", str(e))
            )
        finally:
            self._busy = False

    def fill_missing(self):
        if self._busy:
            messagebox.showinfo("Pixiv", "Busy — stop first.")
            return
        if not self._last_plan or not self._last_pages:
            messagebox.showinfo("Pixiv", "Run Check first.")
            return
        miss = list(self._last_plan.get("missing") or [])
        if not miss:
            messagebox.showinfo("Pixiv", "Nothing missing.")
            return
        folder = self._last_folder
        create_new = folder is None or not Path(folder).is_dir()
        try:
            cookie = self._ensure_cookie_saved()
        except PixivError as e:
            messagebox.showwarning("Pixiv", str(e))
            return
        if not cookie:
            messagebox.showwarning(
                "Pixiv",
                "PHPSESSID required to download originals.\n"
                "Paste it from the browser, Save, then Test login.",
            )
            return
        pics = Path(self.dir_var.get().strip() or DEFAULT_DIR)
        if create_new:
            proposed = gallery_folder_name(
                self._last_meta, illust_id=self._last_plan["illust_id"]
            )
            if not messagebox.askyesno(
                "Pixiv",
                f"No local set found.\n\n"
                f"Create gallery folder under Save-to:\n{pics / proposed}\n\n"
                f"and download {len(miss)} page(s)?",
            ):
                return
        else:
            if not messagebox.askyesno(
                "Pixiv",
                f"Download {len(miss)} missing page(s) into:\n{folder}\n\n"
                f"Pages: {', '.join(f'p{p}' for p in miss[:20])}"
                + ("…" if len(miss) > 20 else ""),
            ):
                return
        self._stop.clear()
        self._busy = True
        self.status.set(
            f"{'Creating + downloading' if create_new else 'Filling'} "
            f"{len(miss)} page(s)…"
        )
        illust = self._last_plan["illust_id"]
        pages = list(self._last_pages)
        gkey = self._last_gallery_key
        meta = dict(self._last_meta or {})
        threading.Thread(
            target=self._fill_worker,
            args=(illust, folder, gkey, pages, pics, meta, create_new),
            daemon=True,
        ).start()

    def _fill_worker(
        self,
        illust: str,
        folder: Path | None,
        gallery_key: str,
        pages: list[dict],
        pics: Path,
        meta: dict,
        create_new: bool,
    ):
        try:
            session = session_from_store(self.store)
            if create_new or folder is None or not Path(folder).is_dir():
                created = ensure_pixiv_gallery(
                    self.store,
                    pics,
                    illust,
                    meta,
                    image_total=len(pages),
                )
                folder = Path(created["folder"])
                gallery_key = created["gallery_key"]
                self._last_folder = folder
                self._last_gallery_key = gallery_key
                self._tab_log(
                    f"created gallery {gallery_key}: {created.get('title')} → {folder}"
                )
            result = fill_missing(
                self.store,
                session,
                illust_id=illust,
                folder=Path(folder),
                gallery_key=gallery_key or None,
                pages=pages,
                should_stop=lambda: self._stop.is_set() or not self._lifecycle_alive,
                on_progress=lambda m: self._tab_log(m),
            )
            persist_session_cookies(self.store, session)
            failed = result.get("failed") or []
            still = result.get("still_missing") or []
            self._tab_log(
                f"done: saved={result.get('saved')} skipped={result.get('skipped')} "
                f"renamed={result.get('renamed')} fp={result.get('fingerprinted')} "
                f"failed={len(failed)}"
            )
            if failed:
                for line in failed[:8]:
                    self._tab_log(f"  {line}")
            local = scan_local_pages(Path(folder), illust)
            plan = plan_fill(
                illust_id=illust, pages=pages, local_pages=local
            )
            self._last_plan = plan
            self._last_folder = Path(folder)
            self._ui_schedule(
                lambda: self._apply_plan_ui(plan, Path(folder), self._last_meta)
            )
            self._ui_schedule(lambda: self._refresh_auth_ui())
            if still or failed:
                self._set_status(
                    f"Finished with gaps — missing {len(still)}, failed {len(failed)}"
                )
            else:
                self._set_status(
                    f"Complete — {result.get('local_count')}/{result.get('remote_count')}"
                )
            log_feed(
                log,
                logging.INFO,
                "Pixiv fill %s: saved=%s still_missing=%s",
                illust,
                result.get("saved"),
                len(still),
            )
        except Exception as e:
            log.exception("pixiv fill failed")
            self._tab_log(f"error: {e}")
            self._set_status(f"Error: {e}")
            self._ui_schedule(
                lambda: messagebox.showerror("Pixiv", str(e))
            )
        finally:
            self._busy = False
