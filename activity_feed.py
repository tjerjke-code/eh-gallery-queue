"""Scrollable activity feed (WishAssistance-style, slimmed)."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from collections import deque
from tkinter import scrolledtext, ttk

from logger import ActivityFeedBus, format_feed_line

_MAX_LINES = 400
_DRAIN_MS = 100
_LEVEL_TAGS = {
    "debug": "#888888",
    "info": "#85c1e9",
    "warning": "#f39c12",
    "error": "#e74c3c",
    "critical": "#ff6b6b",
}


class ActivityFeedPanel(ttk.Frame):
    """Compact feed docked at the bottom of the shell."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self._paused = False
        self._show_operational = tk.BooleanVar(value=True)
        self._show_operational_flag = True
        self._error_count = 0
        self._warning_count = 0
        self._status_var = tk.StringVar(value="Activity feed — idle")
        self._pending: deque[tuple[str, str, int]] = deque()
        self._pending_lock = threading.Lock()
        self._drain_alive = True

        self._build_ui()
        ActivityFeedBus.instance().subscribe(self._on_log_record)
        self.after(_DRAIN_MS, self._drain_pending_feed)

    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(header, text="Activity Feed", font=("Segoe UI", 9, "bold")).pack(
            side="left"
        )
        ttk.Label(
            header,
            textvariable=self._status_var,
            foreground="#888888",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            header,
            text="Operational info",
            variable=self._show_operational,
            command=self._sync_operational_flag,
        ).pack(side="right", padx=4)
        ttk.Button(header, text="Clear", width=7, command=self.clear).pack(
            side="right", padx=2
        )
        self._pause_btn = ttk.Button(
            header, text="Pause", width=7, command=self._toggle_pause
        )
        self._pause_btn.pack(side="right", padx=2)

        self.text = scrolledtext.ScrolledText(
            self,
            height=6,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.text.pack(fill="both", expand=True, padx=6, pady=4)
        for tag, color in _LEVEL_TAGS.items():
            weight = "bold" if tag in ("warning", "error", "critical") else "normal"
            self.text.tag_config(
                tag, foreground=color, font=("Consolas", 9, weight)
            )

    def _sync_operational_flag(self) -> None:
        try:
            self._show_operational_flag = bool(self._show_operational.get())
        except tk.TclError:
            pass

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        try:
            self._pause_btn.configure(text="Resume" if self._paused else "Pause")
        except (tk.TclError, AttributeError):
            pass

    def clear(self) -> None:
        self._error_count = 0
        self._warning_count = 0
        self._status_var.set("Activity feed — cleared")
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.configure(state="disabled")

    def destroy(self) -> None:
        self._drain_alive = False
        try:
            ActivityFeedBus.instance().unsubscribe(self._on_log_record)
        except Exception:
            pass
        super().destroy()

    def _on_log_record(self, record: logging.LogRecord) -> None:
        if self._paused:
            return
        if record.levelno < logging.WARNING and not (
            self._show_operational_flag and getattr(record, "feed", False)
        ):
            return
        line, tag = format_feed_line(record)
        with self._pending_lock:
            self._pending.append((line, tag, record.levelno))

    def _drain_pending_feed(self) -> None:
        if not self._drain_alive:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        batch: list[tuple[str, str, int]] = []
        with self._pending_lock:
            while self._pending:
                batch.append(self._pending.popleft())
        for line, tag, levelno in batch:
            self._append_line(line, tag, levelno)
        try:
            self.after(_DRAIN_MS, self._drain_pending_feed)
        except tk.TclError:
            self._drain_alive = False

    def _append_line(self, line: str, tag: str, levelno: int) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.text.configure(state="normal")
        self.text.insert(tk.END, line + "\n", tag)
        self.text.see(tk.END)
        self.text.configure(state="disabled")
        if levelno >= logging.ERROR:
            self._error_count += 1
        elif levelno >= logging.WARNING:
            self._warning_count += 1
        parts = []
        if self._error_count:
            parts.append(f"{self._error_count} error(s)")
        if self._warning_count:
            parts.append(f"{self._warning_count} warning(s)")
        self._status_var.set(
            "Activity feed — " + (", ".join(parts) if parts else "idle")
        )
