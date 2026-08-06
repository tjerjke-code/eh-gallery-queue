"""EH Gallery Queue entry — durable Tk shell with Ctrl+R hot reload.

Mirrors WishAssistance: keep the process + window alive, reload modules,
tear down the UI frame, rebuild from DB. DB connect runs off the UI thread
with a login timeout so a busy SQL Server cannot freeze startup.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

# Modules reloaded on Ctrl+R (deps first, UI last).
RELOAD_DEPS = (
    'logger',
    'db',
    'activity_feed',
    'eh_hash_check',
    'eh_title_search',
    'local_import',
    'fs_links',
    'app',
)


class HotReloadShell(tk.Tk):
    """Process-owned root. Holds the DB store across reloads."""

    def __init__(self):
        super().__init__()
        self.title('EH Gallery Queue')
        self.geometry('900x700')
        self.minsize(640, 520)

        self.store = None
        self.app_frame = None
        self.feed_panel = None
        self._content = None
        self._reloading = False
        self._db_booting = False

        self.bind('<Control-r>', self._on_reload_key)
        self.bind('<Control-R>', self._on_reload_key)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

        self._content = ttk.Frame(self)
        # Feed first (side=bottom), then content expands into remaining space.
        self._mount_feed()
        self._content.pack(fill='both', expand=True)
        self._mount_app(announce=False)
        # Defer DB so the window paints immediately (was freezing on ODBC).
        self.after(50, self._boot_store_async)

    def _on_reload_key(self, _event=None):
        self.reload_app()
        return 'break'

    def _log(self):
        from logger import get_logger, log_feed

        return get_logger('shell'), log_feed

    def _boot_store_async(self):
        if self._db_booting:
            return
        self._db_booting = True
        log, log_feed = self._log()
        log_feed(log, logging.INFO, 'Connecting to EH database…')

        def work():
            store = None
            err = None
            try:
                import db

                store = db.QueueStore()
            except Exception as e:
                err = e
            self.after(0, lambda: self._on_db_ready(store, err))

        threading.Thread(target=work, daemon=True, name='eh-db-boot').start()

    def _on_db_ready(self, store, err):
        self._db_booting = False
        log, log_feed = self._log()
        if err is not None:
            self.store = None
            log.exception('EH database unavailable: %s', err)
            messagebox.showwarning(
                'Database',
                f'Could not connect to EH database.\n'
                f'Queue will not persist until reload succeeds.\n\n{err}',
            )
            if self.app_frame is not None:
                try:
                    self.app_frame.ui_log(f'DB unavailable — memory-only: {err}')
                except Exception:
                    pass
            return

        old = self.store
        self.store = store
        if old is not None and old is not store:
            try:
                old.close()
            except Exception:
                pass
        src = getattr(store, '_parts', {}).get('source', '?')
        log_feed(log, logging.INFO, 'EH database ready (%s)', src)
        # Remount app so it hydrates queue from DB.
        self._remount_app_with_store(announce=False)

    def _remount_app_with_store(self, *, announce: bool):
        frame = self.app_frame
        if frame is not None:
            if hasattr(frame, 'prepare_for_reload'):
                frame.prepare_for_reload()
            try:
                frame.destroy()
            except tk.TclError:
                pass
            self.app_frame = None
        self._mount_app(announce=announce)

    def _reload_module(self, mod_name: str):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
            print(f'[hot-reload] reloaded {mod_name}', flush=True)
        else:
            importlib.import_module(mod_name)
            print(f'[hot-reload] imported {mod_name}', flush=True)

    def _mount_feed(self):
        if self.feed_panel is not None:
            try:
                self.feed_panel.destroy()
            except tk.TclError:
                pass
            self.feed_panel = None
        from activity_feed import ActivityFeedPanel

        self.feed_panel = ActivityFeedPanel(self)
        self.feed_panel.pack(fill='x', side='bottom')

    def _mount_app(self, *, announce: bool):
        import app as app_mod

        parent = self._content if self._content is not None else self
        self.app_frame = app_mod.App(parent, store=self.store)
        self.app_frame.pack(fill='both', expand=True)
        if announce:
            from logger import get_logger, log_feed

            log_feed(
                get_logger('shell'),
                logging.INFO,
                'Hot reload complete — app + db refreshed',
            )
            self.app_frame.set_status(
                f'Idle — {len(self.app_frame._queue_urls)} in queue (reloaded)'
            )

    def reload_app(self, _event=None):
        """Stop workers, reload modules, rebuild UI (WishAssistance Ctrl+R)."""
        if self._reloading or self._db_booting:
            return
        self._reloading = True
        try:
            print('[hot-reload] starting…', flush=True)
            frame = self.app_frame
            if frame is not None:
                if hasattr(frame, 'prepare_for_reload'):
                    frame.prepare_for_reload()
                try:
                    frame.destroy()
                except tk.TclError:
                    pass
                self.app_frame = None

            for mod_name in RELOAD_DEPS:
                self._reload_module(mod_name)

            try:
                import logger as logger_mod

                if hasattr(logger_mod, 'rebind_after_reload'):
                    logger_mod.rebind_after_reload()
            except Exception:
                pass

            self._mount_feed()

            # Recreate store off the UI thread so ODBC cannot freeze Ctrl+R.
            old = self.store
            self.store = None
            self._mount_app(announce=True)

            def work():
                store = None
                err = None
                try:
                    import db

                    store = db.QueueStore()
                except Exception as e:
                    err = e
                self.after(0, lambda: self._on_reload_db_ready(store, err, old))

            threading.Thread(target=work, daemon=True, name='eh-db-reload').start()
            print('[hot-reload] UI refreshed; DB reconnecting…', flush=True)
        except Exception as e:
            messagebox.showerror(
                'Reload Failure',
                f'Failed to reload code changes:\n\n{e}',
            )
            print(f'[hot-reload] crash: {e}', flush=True)
            if self.app_frame is None:
                try:
                    self._mount_app(announce=False)
                except Exception:
                    pass
            self._reloading = False

    def _on_reload_db_ready(self, store, err, old):
        log, log_feed = self._log()
        if err is not None:
            self.store = old
            log.warning('Reload DB reconnect failed — keeping previous store: %s', err)
            messagebox.showwarning(
                'Hot Reload',
                f'Reloaded UI, but DB reconnect failed — keeping previous store.\n\n{err}',
            )
            if self.store is not None:
                self._remount_app_with_store(announce=False)
            self._reloading = False
            return

        self.store = store
        if old is not None and old is not store:
            try:
                old.close()
            except Exception:
                pass
        log_feed(log, logging.INFO, 'Hot reload DB reconnect OK')
        self._remount_app_with_store(announce=False)
        self._reloading = False
        print('[hot-reload] complete', flush=True)

    def on_close(self):
        frame = self.app_frame
        if frame is not None and getattr(frame, '_worker', None):
            worker = frame._worker
            if worker and worker.is_alive():
                if not messagebox.askokcancel(
                    'Quit', 'Downloader is running. Stop and quit?'
                ):
                    return
        if frame is not None and hasattr(frame, 'prepare_for_reload'):
            try:
                frame.prepare_for_reload()
            except Exception:
                pass
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
        self.destroy()


def main():
    HotReloadShell().mainloop()


if __name__ == '__main__':
    main()
