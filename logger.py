"""Application logging + activity-feed bus (WishAssistance-style, slimmed).

- ``logs/app.log`` — rotating file
- stderr when running under ``python`` (silent under ``pythonw``)
- ``log_feed()`` / WARNING+ → UI activity feed
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGGER_ROOT_NAME = "EHGalleryQueue"
PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_FEED_FORMAT = "%(asctime)s [%(levelname)s] %(short_name)s: %(message)s"


class ActivityFeedFilter(logging.Filter):
    """Pass warnings/errors always; INFO only when marked ``feed=True``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return bool(getattr(record, "feed", False))


class ActivityFeedBus:
    """Thread-safe pub/sub for UI activity feed subscribers."""

    _instance: ActivityFeedBus | None = None

    def __init__(self) -> None:
        self._listeners: list[Callable[[logging.LogRecord], None]] = []
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> ActivityFeedBus:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_reload(cls) -> None:
        """Drop singleton after ``importlib.reload`` so a fresh bus is used."""
        cls._instance = None

    def subscribe(self, callback: Callable[[logging.LogRecord], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[logging.LogRecord], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def publish(self, record: logging.LogRecord) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record)
            except Exception:
                pass


class UiFeedHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.addFilter(ActivityFeedFilter())
        self.setFormatter(logging.Formatter(_FEED_FORMAT, datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            record.short_name = shorten_logger_name(record.name)
            record.feed_line = self.format(record)
            ActivityFeedBus.instance().publish(record)
        except Exception:
            self.handleError(record)


def _configure_root() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_ROOT_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(_LOG_FORMAT)
    file_handler = TimedRotatingFileHandler(
        filename=str(LOG_FILE),
        when="H",
        interval=1,
        backupCount=48,
        encoding="utf-8",
        delay=True,
    )
    file_handler.suffix = "%Y-%m-%d_%H"
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.addHandler(UiFeedHandler())
    return logger


app_logger = _configure_root()


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(LOGGER_ROOT_NAME)
    if name.startswith(f"{LOGGER_ROOT_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_ROOT_NAME}.{name}")


def shorten_logger_name(name: str) -> str:
    prefix = f"{LOGGER_ROOT_NAME}."
    if name.startswith(prefix):
        name = name[len(prefix):]
    return name


def format_feed_line(record: logging.LogRecord) -> tuple[str, str]:
    short_name = shorten_logger_name(record.name)
    stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
    line = f"{stamp} [{record.levelname}] {short_name}: {record.getMessage()}"
    tag = record.levelname.lower()
    if tag not in ("debug", "info", "warning", "error", "critical"):
        tag = "info"
    return line, tag


def log_feed(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args,
    exc_info: bool | BaseException | None = None,
    **kwargs,
) -> None:
    """Log a line that also appears in the activity feed (INFO+)."""
    extra = dict(kwargs.pop("extra", {}) or {})
    extra["feed"] = True
    logger.log(level, msg, *args, exc_info=exc_info, extra=extra, **kwargs)


def rebind_after_reload() -> logging.Logger:
    """Call after ``importlib.reload(logger)`` to restore handlers + fresh bus."""
    ActivityFeedBus.reset_for_reload()
    root = logging.getLogger(LOGGER_ROOT_NAME)
    root.handlers.clear()
    return _configure_root()
