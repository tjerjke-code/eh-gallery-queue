"""MSSQL persistence for EH gallery queue.

Uses a dedicated ``EH`` database on the same SQL Server as WishAssistance.
In-progress queue + per-image rows are temporary; successful galleries leave
only a top-level ``galleries`` row for dedupe.
"""

from __future__ import annotations

import configparser
import re
import threading
from pathlib import Path

import pyodbc

_REPO = Path(__file__).resolve().parent
_GALLERY_RE = re.compile(r"/g/(\d+)/([0-9a-fA-F]+)")
# EH image page: /s/{img_key}/{gallery_id}-{page}
_IMAGE_PAGE_RE = re.compile(
    r"/s/([0-9a-fA-F]+)/(\d+)-(\d+)", re.IGNORECASE
)
_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_WISH_SETTINGS = (
    Path(r"C:\Users\Spleen\PycharmProjects\WishAsisstance\secrets\settings.cnf"),
    Path(r"D:\PycharmProjects\WishAsisstance\secrets\settings.cnf"),
)


def gallery_key_from_url(url: str) -> str | None:
    m = _GALLERY_RE.search(url or "")
    return m.group(1) if m else None


def gallery_token_from_url(url: str) -> str | None:
    m = _GALLERY_RE.search(url or "")
    return m.group(2) if m else None


def image_page_id(page_url: str) -> str | None:
    """Stable identity for an EH image page (not the display filename)."""
    m = _IMAGE_PAGE_RE.search(page_url or "")
    if not m:
        return None
    return f"{m.group(1).lower()}:{m.group(2)}:{m.group(3)}"


def _safe_filename(name: str) -> str:
    name = _WIN_BAD.sub("_", (name or "").strip(" ."))
    return (name[:180] or "image")


def index_pad_width(total: int) -> int:
    """Digit width for order prefixes: ≤99 → 2, ≤999 → 3, …"""
    return max(2, len(str(max(1, int(total)))))


def strip_order_prefix(filename: str, width: int) -> str:
    """Remove leading zero-padded ``NNN_`` only when digit count == width."""
    m = re.match(r"^(\d+)_(.*)$", filename or "", flags=re.DOTALL)
    if not m:
        return filename
    if len(m.group(1)) != int(width):
        return filename
    return m.group(2) or filename


def apply_order_prefix(index_1based: int, filename: str, total: int) -> str:
    """``{idx:0Wd}_{bare}`` — keeps pretty thumb names, sortable by gallery order."""
    w = index_pad_width(total)
    bare = strip_order_prefix(filename, w)
    bare = _safe_filename(bare)
    return f"{int(index_1based):0{w}d}_{bare}"[:260]


def normalize_image_links(
    links: list[tuple[str, str]],
    *,
    total: int | None = None,
) -> list[tuple[str, str]]:
    """Keep pretty EH names; ignore same page; suffix name collisions; add order idx.

    - Same image page listed twice → keep first.
    - First use of a display name → keep as-is (``0.jpg``).
    - Later different pages with the same name → ``0_1.jpg``, ``0_2.jpg``, …
    - Then prefix ``01_`` / ``001_`` / … from gallery order (pad by ``total``).
    """
    seen_pages: set[str] = set()
    used_names: set[str] = set()
    staged: list[tuple[str, str]] = []
    for page_url, name in links:
        page_url = (page_url or "").strip()
        if not page_url:
            continue
        pid = image_page_id(page_url) or page_url
        if pid in seen_pages:
            continue
        seen_pages.add(pid)

        base = _safe_filename(name)
        if base not in used_names:
            candidate = base
        else:
            stem, dot, ext = base.rpartition(".")
            if not dot:
                stem, ext = base, ""
            n = 1
            while True:
                candidate = f"{stem}_{n}.{ext}" if dot else f"{stem}_{n}"
                if candidate not in used_names:
                    break
                n += 1
        used_names.add(candidate)
        staged.append((page_url, candidate))

    width_total = max(len(staged), int(total or 0), 1)
    return [
        (url, apply_order_prefix(i, fn, width_total))
        for i, (url, fn) in enumerate(staged, start=1)
    ]


def _load_odbc_parts() -> dict[str, str]:
    """Local secrets first, else WishAssistance settings (database forced to EH)."""
    local = _REPO / "secrets" / "settings.cnf"
    candidates = [local, *_WISH_SETTINGS]
    for path in candidates:
        if not path.is_file():
            continue
        cfg = configparser.RawConfigParser()
        cfg.read(path)
        section = "EH_DB" if cfg.has_section("EH_DB") else "WIKI_DB"
        if not cfg.has_section(section):
            continue
        c = dict(cfg[section])
        return {
            "driver": c["driver"],
            "server": c["server"],
            "database": "EH",
            "uid": c["uid"],
            "pwd": c["pwd"],
            "source": str(path),
        }
    raise FileNotFoundError(
        "No DB settings found. Copy secrets/settings.cnf.example to "
        "secrets/settings.cnf (or keep WishAssistance secrets/settings.cnf)."
    )


# Keep UI from freezing forever when SQL Server is busy / wedged.
_LOGIN_TIMEOUT_S = 5


def _conn_str(parts: dict[str, str], database: str | None = None) -> str:
    return (
        f"DRIVER={parts['driver']};"
        f"SERVER={parts['server']};"
        f"DATABASE={database or parts['database']};"
        f"UID={parts['uid']};"
        f"PWD={parts['pwd']};"
        f"LoginTimeout={_LOGIN_TIMEOUT_S};"
        f"TrustServerCertificate=yes;"
    )


class QueueStore:
    """Thread-safe EH queue / gallery store."""

    def __init__(self):
        self._parts = _load_odbc_parts()
        self._lock = threading.RLock()
        self._local = threading.local()
        self._ensure_database()
        self._ensure_schema()

    def _conn(self) -> pyodbc.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pyodbc.connect(
                _conn_str(self._parts),
                autocommit=False,
                timeout=_LOGIN_TIMEOUT_S,
            )
            self._local.conn = conn
        return conn

    def _ensure_database(self) -> None:
        # CREATE DATABASE cannot run inside an explicit multi-statement txn
        # on some setups; use a short-lived autocommit connection to master.
        master = pyodbc.connect(
            _conn_str(self._parts, "master"),
            autocommit=True,
            timeout=_LOGIN_TIMEOUT_S,
        )
        try:
            cur = master.cursor()
            exists = cur.execute(
                "SELECT 1 FROM sys.databases WHERE name = N'EH'"
            ).fetchone()
            if not exists:
                cur.execute("CREATE DATABASE [EH]")
        finally:
            master.close()

    def _ensure_schema(self) -> None:
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                IF OBJECT_ID(N'dbo.galleries', N'U') IS NULL
                CREATE TABLE dbo.galleries (
                    id            INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    gallery_key   NVARCHAR(64)  NOT NULL,
                    token         NVARCHAR(64)  NULL,
                    url           NVARCHAR(512) NOT NULL,
                    title         NVARCHAR(256) NULL,
                    out_dir       NVARCHAR(512) NULL,
                    image_total   INT NULL,
                    saved         INT NULL,
                    skipped       INT NULL,
                    failed        INT NULL,
                    completed_at  DATETIME2 NOT NULL
                        CONSTRAINT DF_galleries_completed
                        DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT UQ_galleries_key UNIQUE (gallery_key)
                );

                IF OBJECT_ID(N'dbo.queue_items', N'U') IS NULL
                CREATE TABLE dbo.queue_items (
                    id            INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    gallery_key   NVARCHAR(64)  NOT NULL,
                    url           NVARCHAR(512) NOT NULL,
                    title         NVARCHAR(256) NULL,
                    out_dir       NVARCHAR(512) NULL,
                    status        NVARCHAR(32)  NOT NULL,
                    position      INT NOT NULL
                        CONSTRAINT DF_queue_position DEFAULT (0),
                    image_total   INT NULL,
                    saved         INT NOT NULL
                        CONSTRAINT DF_queue_saved DEFAULT (0),
                    skipped       INT NOT NULL
                        CONSTRAINT DF_queue_skipped DEFAULT (0),
                    failed        INT NOT NULL
                        CONSTRAINT DF_queue_failed DEFAULT (0),
                    last_error    NVARCHAR(1000) NULL,
                    created_at    DATETIME2 NOT NULL
                        CONSTRAINT DF_queue_created DEFAULT (SYSUTCDATETIME()),
                    updated_at    DATETIME2 NOT NULL
                        CONSTRAINT DF_queue_updated DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT UQ_queue_gallery_key UNIQUE (gallery_key)
                );

                IF OBJECT_ID(N'dbo.queue_images', N'U') IS NULL
                CREATE TABLE dbo.queue_images (
                    id            BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    queue_id      INT NOT NULL
                        CONSTRAINT FK_queue_images_item
                        REFERENCES dbo.queue_items(id) ON DELETE CASCADE,
                    page_url      NVARCHAR(512) NOT NULL,
                    filename      NVARCHAR(260) NOT NULL,
                    status        NVARCHAR(32)  NOT NULL,
                    last_error    NVARCHAR(1000) NULL,
                    updated_at    DATETIME2 NOT NULL
                        CONSTRAINT DF_qimg_updated DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT UQ_queue_images_file UNIQUE (queue_id, filename)
                );

                IF OBJECT_ID(N'dbo.app_settings', N'U') IS NULL
                CREATE TABLE dbo.app_settings (
                    [key]   NVARCHAR(64)  NOT NULL PRIMARY KEY,
                    value   NVARCHAR(512) NOT NULL
                );

                -- Exact-file identity (20 bytes) + optional 64-bit dHash for later resize-tolerant match.
                -- ~40B/row without path; sample_path only for the first seen copy.
                IF OBJECT_ID(N'dbo.image_fingerprints', N'U') IS NULL
                CREATE TABLE dbo.image_fingerprints (
                    sha1         BINARY(20)   NOT NULL PRIMARY KEY,
                    byte_len     INT          NOT NULL,
                    dhash        BIGINT       NULL,
                    sample_path  NVARCHAR(400) NULL,
                    gallery_key  NVARCHAR(64)  NULL,
                    seen_count   INT          NOT NULL
                        CONSTRAINT DF_fp_seen DEFAULT (1),
                    created_at   DATETIME2    NOT NULL
                        CONSTRAINT DF_fp_created DEFAULT (SYSUTCDATETIME()),
                    updated_at   DATETIME2    NOT NULL
                        CONSTRAINT DF_fp_updated DEFAULT (SYSUTCDATETIME())
                );

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_queue_items_status'
                      AND object_id = OBJECT_ID(N'dbo.queue_items')
                )
                CREATE INDEX IX_queue_items_status
                    ON dbo.queue_items (status, position);

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_image_fp_dhash'
                      AND object_id = OBJECT_ID(N'dbo.image_fingerprints')
                )
                CREATE INDEX IX_image_fp_dhash
                    ON dbo.image_fingerprints (dhash)
                    WHERE dhash IS NOT NULL;

                -- Meta / alternate names for the same bytes across galleries (soft rename history).
                IF OBJECT_ID(N'dbo.image_name_aliases', N'U') IS NULL
                CREATE TABLE dbo.image_name_aliases (
                    id           BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    sha1         BINARY(20)    NOT NULL
                        CONSTRAINT FK_fp_alias_sha
                        REFERENCES dbo.image_fingerprints(sha1),
                    name         NVARCHAR(260) NOT NULL,
                    bare_name    NVARCHAR(260) NULL,
                    gallery_key  NVARCHAR(64)  NOT NULL
                        CONSTRAINT DF_fp_alias_gkey DEFAULT (N''),
                    sample_path  NVARCHAR(400) NULL,
                    created_at   DATETIME2     NOT NULL
                        CONSTRAINT DF_fp_alias_created DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT UQ_fp_alias UNIQUE (sha1, name, gallery_key)
                );

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_fp_alias_bare'
                      AND object_id = OBJECT_ID(N'dbo.image_name_aliases')
                )
                CREATE INDEX IX_fp_alias_bare
                    ON dbo.image_name_aliases (bare_name)
                    WHERE bare_name IS NOT NULL;

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_fp_alias_sha'
                      AND object_id = OBJECT_ID(N'dbo.image_name_aliases')
                )
                CREATE INDEX IX_fp_alias_sha
                    ON dbo.image_name_aliases (sha1);

                -- Manual vs auto-discovered queue rows (auto always sorted last).
                IF COL_LENGTH(N'dbo.queue_items', N'source') IS NULL
                ALTER TABLE dbo.queue_items ADD
                    source NVARCHAR(16) NOT NULL
                        CONSTRAINT DF_queue_source DEFAULT (N'manual');

                -- Slow EH f_shash scan queue (WishAssistance asset_download_queue style).
                IF OBJECT_ID(N'dbo.eh_sha_checks', N'U') IS NULL
                CREATE TABLE dbo.eh_sha_checks (
                    sha1         BINARY(20)   NOT NULL PRIMARY KEY
                        CONSTRAINT FK_eh_sha_checks_fp
                        REFERENCES dbo.image_fingerprints(sha1),
                    status       NVARCHAR(32) NOT NULL
                        CONSTRAINT DF_eh_sha_status DEFAULT (N'pending'),
                    match_count  INT NOT NULL
                        CONSTRAINT DF_eh_sha_matches DEFAULT (0),
                    last_error   NVARCHAR(1000) NULL,
                    checked_at   DATETIME2 NULL,
                    created_at   DATETIME2 NOT NULL
                        CONSTRAINT DF_eh_sha_created DEFAULT (SYSUTCDATETIME()),
                    updated_at   DATETIME2 NOT NULL
                        CONSTRAINT DF_eh_sha_updated DEFAULT (SYSUTCDATETIME())
                );

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_eh_sha_checks_pending'
                      AND object_id = OBJECT_ID(N'dbo.eh_sha_checks')
                )
                CREATE INDEX IX_eh_sha_checks_pending
                    ON dbo.eh_sha_checks (status, created_at)
                    WHERE status = N'pending';

                IF OBJECT_ID(N'dbo.eh_sha_match_galleries', N'U') IS NULL
                CREATE TABLE dbo.eh_sha_match_galleries (
                    sha1         BINARY(20)    NOT NULL
                        CONSTRAINT FK_eh_sha_match_check
                        REFERENCES dbo.eh_sha_checks(sha1),
                    gallery_key  NVARCHAR(64)  NOT NULL,
                    url          NVARCHAR(512) NOT NULL,
                    title        NVARCHAR(256) NULL,
                    created_at   DATETIME2 NOT NULL
                        CONSTRAINT DF_eh_sha_match_created
                        DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT PK_eh_sha_match PRIMARY KEY (sha1, gallery_key)
                );

                -- Duped tab: user appointed a canonical home for this SHA.
                IF COL_LENGTH(N'dbo.image_fingerprints', N'home_decided') IS NULL
                ALTER TABLE dbo.image_fingerprints ADD
                    home_decided BIT NOT NULL
                        CONSTRAINT DF_fp_home_decided DEFAULT (0);
                """
            )
            self._conn().commit()

    def lookup_sha1(self, digest: bytes) -> dict | None:
        """Return first-seen row for an exact file hash, or None."""
        if not digest or len(digest) != 20:
            return None
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT sample_path, byte_len, gallery_key, seen_count, dhash
                FROM dbo.image_fingerprints
                WHERE sha1 = ?
                """,
                digest,
            ).fetchone()
        if not row:
            return None
        return {
            "sample_path": row[0],
            "byte_len": int(row[1]) if row[1] is not None else None,
            "gallery_key": row[2],
            "seen_count": int(row[3] or 0),
            "dhash": int(row[4]) if row[4] is not None else None,
        }

    def list_name_aliases(self, digest: bytes) -> list[dict]:
        """All known names for this content (cross-gallery meta)."""
        if not digest or len(digest) != 20:
            return []
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT name, bare_name, gallery_key, sample_path, created_at
                FROM dbo.image_name_aliases
                WHERE sha1 = ?
                ORDER BY created_at ASC, id ASC
                """,
                digest,
            ).fetchall()
        return [
            {
                "name": r[0],
                "bare_name": r[1],
                "gallery_key": r[2] or "",
                "sample_path": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def record_name_alias(
        self,
        digest: bytes,
        *,
        name: str,
        bare_name: str | None = None,
        gallery_key: str | None = None,
        sample_path: str | None = None,
    ) -> None:
        """Remember a filename variant for this sha1 (does not move files).

        If ``gallery_key`` is a *new* peer for an already-decided SHA, clear
        ``home_decided`` so Duped Undecided-only shows it again for review.
        Extra names under an already-known gallery do not reopen.
        """
        if not digest or len(digest) != 20:
            return
        name = (name or "").strip()[:260]
        if not name:
            return
        bare = (bare_name or "").strip()[:260] or None
        gkey = (gallery_key or "").strip()[:64] or ""
        path = (sample_path or "").strip()[:400] or None
        with self._lock:
            cur = self._conn().cursor()
            # Fingerprint row must exist (FK).
            if not cur.execute(
                "SELECT 1 FROM dbo.image_fingerprints WHERE sha1 = ?", digest
            ).fetchone():
                return
            new_peer = False
            if gkey:
                known = cur.execute(
                    """
                    SELECT 1
                    FROM dbo.image_name_aliases
                    WHERE sha1 = ? AND gallery_key = ?
                    """,
                    digest,
                    gkey,
                ).fetchone()
                new_peer = not known
            cur.execute(
                """
                MERGE dbo.image_name_aliases AS t
                USING (
                    SELECT
                        ? AS sha1,
                        ? AS name,
                        ? AS bare_name,
                        ? AS gallery_key,
                        ? AS sample_path
                ) AS s
                ON t.sha1 = s.sha1
                   AND t.name = s.name
                   AND t.gallery_key = s.gallery_key
                WHEN MATCHED THEN UPDATE SET
                    bare_name = COALESCE(s.bare_name, t.bare_name),
                    sample_path = COALESCE(s.sample_path, t.sample_path)
                WHEN NOT MATCHED THEN INSERT
                    (sha1, name, bare_name, gallery_key, sample_path)
                VALUES
                    (s.sha1, s.name, s.bare_name, s.gallery_key, s.sample_path);
                """,
                digest,
                name,
                bare,
                gkey,
                path,
            )
            if new_peer:
                # New gallery shares this bytes — reopen Duped decision.
                cur.execute(
                    """
                    UPDATE dbo.image_fingerprints
                    SET home_decided = 0,
                        updated_at = SYSUTCDATETIME()
                    WHERE sha1 = ?
                      AND ISNULL(home_decided, 0) = 1
                    """,
                    digest,
                )
            self._conn().commit()

    def register_sha1(
        self,
        digest: bytes,
        byte_len: int,
        *,
        sample_path: str | None = None,
        gallery_key: str | None = None,
        dhash: int | None = None,
        name: str | None = None,
        bare_name: str | None = None,
    ) -> bool:
        """Upsert exact fingerprint (+ optional name alias). Returns True if sha1 was new.

        First-seen ``sample_path`` stays the canonical on-disk copy. Later galleries
        only bump ``seen_count`` and record aliases — soft meta for set matching.

        EH ``f_shash`` is queued **once per SHA-1** (new fingerprint only). Pair
        sightings in other galleries share the same digest and must not re-check.
        """
        if not digest or len(digest) != 20:
            raise ValueError("sha1 digest must be 20 bytes")
        path = (sample_path or "")[:400] or None
        gkey = (gallery_key or "")[:64] or None
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                "SELECT 1 FROM dbo.image_fingerprints WHERE sha1 = ?", digest
            ).fetchone()
            is_new = not row
            if row:
                cur.execute(
                    """
                    UPDATE dbo.image_fingerprints
                    SET seen_count = seen_count + 1,
                        dhash = COALESCE(?, dhash),
                        updated_at = SYSUTCDATETIME()
                    WHERE sha1 = ?
                    """,
                    dhash,
                    digest,
                )
            else:
                cur.execute(
                    """
                    INSERT INTO dbo.image_fingerprints
                        (sha1, byte_len, dhash, sample_path, gallery_key)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    digest,
                    int(byte_len),
                    dhash,
                    path,
                    gkey,
                )
            self._conn().commit()

        alias_name = (name or "").strip() or (
            Path(path).name if path else ""
        )
        if alias_name:
            self.record_name_alias(
                digest,
                name=alias_name,
                bare_name=bare_name,
                gallery_key=gkey,
                sample_path=path,
            )
        # Only brand-new digests → EH f_shash. Existing / pair aliases: never.
        if is_new:
            try:
                self.enqueue_sha_check(digest)
            except Exception:
                pass
        return is_new

    def set_fingerprint_home(
        self,
        digest: bytes,
        *,
        sample_path: str,
        gallery_key: str | None = None,
        decided: bool = True,
    ) -> None:
        """Point canonical on-disk copy at a new real file (after Duped move).

        ``decided=True`` marks the SHA as resolved in the Duped tab so it can
        drop out of the undecided filter.
        """
        if not digest or len(digest) != 20:
            raise ValueError("sha1 digest must be 20 bytes")
        path = (sample_path or "").strip()[:400]
        if not path:
            raise ValueError("sample_path required")
        gkey = (gallery_key or "").strip()[:64] or None
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                UPDATE dbo.image_fingerprints
                SET sample_path = ?,
                    gallery_key = COALESCE(?, gallery_key),
                    home_decided = ?,
                    updated_at = SYSUTCDATETIME()
                WHERE sha1 = ?
                """,
                path,
                gkey,
                1 if decided else 0,
                digest,
            )
            if gkey:
                cur.execute(
                    """
                    UPDATE dbo.image_name_aliases
                    SET sample_path = ?
                    WHERE sha1 = ? AND gallery_key = ?
                    """,
                    path,
                    digest,
                    gkey,
                )
            self._conn().commit()

    def list_dupe_galleries(
        self, *, limit: int = 500, undecided_only: bool = False
    ) -> list[dict]:
        """Galleries that share ≥1 SHA-1 with another gallery (via aliases).

        With ``undecided_only``, only galleries that still have ≥1 shared SHA
        with ``home_decided = 0`` are returned (fully resolved homes drop out).
        """
        having_sql = ""
        if undecided_only:
            having_sql = (
                "HAVING COUNT(DISTINCT CASE "
                "WHEN ISNULL(f.home_decided, 0) = 0 THEN a.sha1 END) > 0"
            )
        with self._lock:
            rows = self._conn().cursor().execute(
                f"""
                SELECT TOP (?)
                    a.gallery_key,
                    COUNT(DISTINCT a.sha1) AS shared_count,
                    COUNT(DISTINCT peer.gallery_key) AS peer_count,
                    MAX(COALESCE(g.title, q.title)) AS title,
                    MAX(COALESCE(g.out_dir, q.out_dir)) AS out_dir,
                    MAX(COALESCE(g.url, q.url)) AS url,
                    COUNT(DISTINCT CASE
                        WHEN ISNULL(f.home_decided, 0) = 0 THEN a.sha1 END
                    ) AS undecided_count
                FROM dbo.image_name_aliases a
                INNER JOIN dbo.image_name_aliases peer
                    ON peer.sha1 = a.sha1
                   AND peer.gallery_key <> a.gallery_key
                   AND peer.gallery_key <> N''
                INNER JOIN dbo.image_fingerprints f ON f.sha1 = a.sha1
                LEFT JOIN dbo.galleries g ON g.gallery_key = a.gallery_key
                LEFT JOIN dbo.queue_items q ON q.gallery_key = a.gallery_key
                WHERE a.gallery_key <> N''
                GROUP BY a.gallery_key
                {having_sql}
                ORDER BY
                    COUNT(DISTINCT CASE
                        WHEN ISNULL(f.home_decided, 0) = 0 THEN a.sha1 END
                    ) DESC,
                    COUNT(DISTINCT a.sha1) DESC,
                    a.gallery_key ASC
                """,
                int(limit),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "gallery_key": r[0],
                    "shared_count": int(r[1] or 0),
                    "peer_count": int(r[2] or 0),
                    "title": r[3],
                    "out_dir": r[4],
                    "url": r[5],
                    "undecided_count": int(r[6] or 0),
                }
            )
        return out

    def list_shared_files_for_gallery(
        self, gallery_key: str, *, undecided_only: bool = False
    ) -> list[dict]:
        """SHA-1s for ``gallery_key`` that also appear under other galleries."""
        key = (gallery_key or "").strip()[:64]
        if not key:
            return []
        undecided_sql = ""
        if undecided_only:
            undecided_sql = "AND ISNULL(f.home_decided, 0) = 0"
        with self._lock:
            cur = self._conn().cursor()
            sha_rows = cur.execute(
                f"""
                SELECT DISTINCT a.sha1, f.sample_path, f.gallery_key, f.byte_len,
                       f.seen_count, ISNULL(f.home_decided, 0)
                FROM dbo.image_name_aliases a
                INNER JOIN dbo.image_fingerprints f ON f.sha1 = a.sha1
                WHERE a.gallery_key = ?
                  AND EXISTS (
                      SELECT 1
                      FROM dbo.image_name_aliases b
                      WHERE b.sha1 = a.sha1
                        AND b.gallery_key <> a.gallery_key
                        AND b.gallery_key <> N''
                  )
                  {undecided_sql}
                ORDER BY a.sha1
                """,
                key,
            ).fetchall()
            if not sha_rows:
                return []
            digests = [bytes(r[0]) for r in sha_rows]
            placeholders = ",".join("?" * len(digests))
            alias_rows = cur.execute(
                f"""
                SELECT sha1, name, bare_name, gallery_key, sample_path
                FROM dbo.image_name_aliases
                WHERE sha1 IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                *digests,
            ).fetchall()
        by_sha: dict[bytes, list[dict]] = {d: [] for d in digests}
        for r in alias_rows:
            digest = bytes(r[0])
            by_sha.setdefault(digest, []).append(
                {
                    "name": r[1],
                    "bare_name": r[2],
                    "gallery_key": r[3] or "",
                    "sample_path": r[4],
                }
            )
        out = []
        for r in sha_rows:
            digest = bytes(r[0])
            out.append(
                {
                    "sha1": digest,
                    "sha1_hex": digest.hex(),
                    "sample_path": r[1],
                    "home_gallery_key": r[2],
                    "byte_len": int(r[3]) if r[3] is not None else None,
                    "seen_count": int(r[4] or 0),
                    "home_decided": bool(r[5]),
                    "aliases": by_sha.get(digest, []),
                }
            )
        return out

    def list_gallery_ordered_names(self, gallery_key: str) -> list[dict]:
        """Alias filenames for a gallery, sorted for neighbor context."""
        key = (gallery_key or "").strip()[:64]
        if not key:
            return []
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT name, bare_name, sample_path
                FROM dbo.image_name_aliases
                WHERE gallery_key = ?
                ORDER BY name ASC, id ASC
                """,
                key,
            ).fetchall()
        return [
            {
                "name": r[0] or "",
                "bare_name": r[1] or "",
                "sample_path": r[2],
            }
            for r in rows
            if r[0]
        ]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn().cursor().execute(
                "SELECT value FROM dbo.app_settings WHERE [key] = ?", key
            ).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                MERGE dbo.app_settings AS t
                USING (SELECT ? AS [key], ? AS value) AS s
                ON t.[key] = s.[key]
                WHEN MATCHED THEN UPDATE SET value = s.value
                WHEN NOT MATCHED THEN INSERT ([key], value) VALUES (s.[key], s.value);
                """,
                key,
                value,
            )
            self._conn().commit()

    def is_completed(self, url: str) -> bool:
        key = gallery_key_from_url(url)
        if not key:
            return False
        with self._lock:
            row = self._conn().cursor().execute(
                "SELECT 1 FROM dbo.galleries WHERE gallery_key = ?", key
            ).fetchone()
            return bool(row)

    def list_active_queue(self) -> list[dict]:
        """Pending / stopped / failed / running items, ordered for the UI.

        Manual rows sort before auto (source), then by position.
        """
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT id, gallery_key, url, title, out_dir, status, position,
                       image_total, saved, skipped, failed, last_error,
                       COALESCE(source, N'manual')
                FROM dbo.queue_items
                WHERE status IN (N'pending', N'stopped', N'failed', N'running')
                ORDER BY
                    CASE WHEN COALESCE(source, N'manual') = N'auto' THEN 1 ELSE 0 END,
                    position ASC,
                    id ASC
                """
            ).fetchall()
        return [
            {
                "id": r[0],
                "gallery_key": r[1],
                "url": r[2],
                "title": r[3],
                "out_dir": r[4],
                "status": r[5],
                "position": r[6],
                "image_total": r[7],
                "saved": r[8],
                "skipped": r[9],
                "failed": r[10],
                "last_error": r[11],
                "source": (r[12] or "manual"),
            }
            for r in rows
        ]

    def enqueue(
        self,
        url: str,
        position: int | None = None,
        *,
        source: str = "manual",
        title: str | None = None,
    ) -> int:
        """Insert a gallery URL. ``source='auto'`` always appends after manuals."""
        key = gallery_key_from_url(url)
        if not key:
            raise ValueError("URL is not an e-hentai /g/ gallery link")
        token = gallery_token_from_url(url)
        src = "auto" if (source or "").strip().lower() == "auto" else "manual"
        title_s = (title or "").strip()[:256] or None
        with self._lock:
            cur = self._conn().cursor()
            existing = cur.execute(
                "SELECT id, status FROM dbo.queue_items WHERE gallery_key = ?",
                key,
            ).fetchone()
            if existing:
                if title_s:
                    cur.execute(
                        """
                        UPDATE dbo.queue_items
                        SET title = COALESCE(title, ?),
                            updated_at = SYSUTCDATETIME()
                        WHERE gallery_key = ?
                        """,
                        title_s,
                        key,
                    )
                    self._conn().commit()
                return int(existing[0])
            if position is None:
                if src == "auto":
                    row = cur.execute(
                        "SELECT ISNULL(MAX(position), -1) + 1 FROM dbo.queue_items"
                    ).fetchone()
                    position = int(row[0]) if row else 0
                else:
                    # Insert after last manual; bump autos so they stay at the end.
                    row = cur.execute(
                        """
                        SELECT ISNULL(MAX(position), -1)
                        FROM dbo.queue_items
                        WHERE COALESCE(source, N'manual') <> N'auto'
                        """
                    ).fetchone()
                    position = int(row[0]) + 1 if row else 0
                    cur.execute(
                        """
                        UPDATE dbo.queue_items
                        SET position = position + 1,
                            updated_at = SYSUTCDATETIME()
                        WHERE COALESCE(source, N'manual') = N'auto'
                          AND position >= ?
                        """,
                        position,
                    )
            cur.execute(
                """
                INSERT INTO dbo.queue_items
                    (gallery_key, url, title, status, position, source)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, N'pending', ?, ?)
                """,
                key,
                url.strip(),
                title_s,
                position,
                src,
            )
            qid = int(cur.fetchone()[0])
            _ = token
            self._conn().commit()
            return qid

    def lookup_gallery_title(self, gallery_key: str) -> str | None:
        """Best-known title for a gallery (queue, EH hash hits, or completed)."""
        key = (gallery_key or "").strip()
        if not key:
            return None
        with self._lock:
            cur = self._conn().cursor()
            for sql in (
                """
                SELECT title FROM dbo.queue_items
                WHERE gallery_key = ? AND title IS NOT NULL AND LTRIM(RTRIM(title)) <> N''
                """,
                """
                SELECT TOP 1 title FROM dbo.eh_sha_match_galleries
                WHERE gallery_key = ? AND title IS NOT NULL AND LTRIM(RTRIM(title)) <> N''
                ORDER BY created_at DESC
                """,
                """
                SELECT title FROM dbo.galleries
                WHERE gallery_key = ? AND title IS NOT NULL AND LTRIM(RTRIM(title)) <> N''
                """,
            ):
                row = cur.execute(sql, key).fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
        return None

    def enqueue_sha_check(self, digest: bytes) -> bool:
        """Queue a fingerprint for EH f_shash scan. Returns True if newly pending.

        Idempotent per SHA-1: if any ``eh_sha_checks`` row exists (pending/done/error),
        do not insert again. Pair aliases of the same bytes share this digest.
        """
        if not digest or len(digest) != 20:
            return False
        with self._lock:
            cur = self._conn().cursor()
            if not cur.execute(
                "SELECT 1 FROM dbo.image_fingerprints WHERE sha1 = ?", digest
            ).fetchone():
                return False
            row = cur.execute(
                "SELECT status FROM dbo.eh_sha_checks WHERE sha1 = ?", digest
            ).fetchone()
            if row:
                return False
            cur.execute(
                """
                INSERT INTO dbo.eh_sha_checks (sha1, status)
                VALUES (?, N'pending')
                """,
                digest,
            )
            self._conn().commit()
            return True

    def seed_pending_sha_checks(self, *, limit: int = 5000) -> int:
        """Enqueue unchecked fingerprints (batch). Returns rows inserted."""
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                INSERT INTO dbo.eh_sha_checks (sha1, status)
                SELECT TOP (?) f.sha1, N'pending'
                FROM dbo.image_fingerprints f
                WHERE NOT EXISTS (
                    SELECT 1 FROM dbo.eh_sha_checks c WHERE c.sha1 = f.sha1
                )
                ORDER BY f.created_at ASC
                """,
                int(limit),
            )
            n = int(cur.rowcount or 0)
            self._conn().commit()
            return n

    def claim_next_sha_check(self) -> dict | None:
        """Pick oldest pending sha check (status stays pending until finish)."""
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                """
                SELECT TOP (1) c.sha1, f.gallery_key, f.sample_path
                FROM dbo.eh_sha_checks c
                INNER JOIN dbo.image_fingerprints f ON f.sha1 = c.sha1
                WHERE c.status = N'pending'
                ORDER BY c.created_at ASC, c.sha1 ASC
                """
            ).fetchone()
        if not row:
            return None
        return {
            "sha1": bytes(row[0]),
            "gallery_key": row[1],
            "sample_path": row[2],
        }

    def finish_sha_check(
        self,
        digest: bytes,
        *,
        matches: list[dict] | None = None,
        error: str | None = None,
    ) -> None:
        """Mark check done/error and upsert discovered gallery URLs."""
        if not digest or len(digest) != 20:
            return
        matches = matches or []
        status = "error" if error else "done"
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                UPDATE dbo.eh_sha_checks
                SET status = ?,
                    match_count = ?,
                    last_error = ?,
                    checked_at = SYSUTCDATETIME(),
                    updated_at = SYSUTCDATETIME()
                WHERE sha1 = ?
                """,
                status,
                len(matches),
                (error or "")[:1000] if error else None,
                digest,
            )
            for m in matches:
                key = (m.get("gallery_key") or "").strip()[:64]
                url = (m.get("url") or "").strip()[:512]
                if not key or not url:
                    continue
                title = (m.get("title") or "").strip()[:256] or None
                cur.execute(
                    """
                    MERGE dbo.eh_sha_match_galleries AS t
                    USING (
                        SELECT ? AS sha1, ? AS gallery_key, ? AS url, ? AS title
                    ) AS s
                    ON t.sha1 = s.sha1 AND t.gallery_key = s.gallery_key
                    WHEN MATCHED THEN UPDATE SET
                        url = s.url,
                        title = COALESCE(s.title, t.title)
                    WHEN NOT MATCHED THEN INSERT (sha1, gallery_key, url, title)
                    VALUES (s.sha1, s.gallery_key, s.url, s.title);
                    """,
                    digest,
                    key,
                    url,
                    title,
                )
            self._conn().commit()

    def count_sha_checks(self, status: str = "pending") -> int:
        with self._lock:
            row = self._conn().cursor().execute(
                "SELECT COUNT(*) FROM dbo.eh_sha_checks WHERE status = ?",
                status,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def requeue_sha_check_errors(self, *, error_substr: str | None = None) -> int:
        """Reset ``error`` SHA checks back to ``pending`` (e.g. after a ban storm).

        If ``error_substr`` is set, only rows whose ``last_error`` contains it
        (case-insensitive) are requeued.
        """
        with self._lock:
            cur = self._conn().cursor()
            if error_substr:
                needle = f"%{error_substr}%"
                cur.execute(
                    """
                    UPDATE dbo.eh_sha_checks
                    SET status = N'pending',
                        last_error = NULL,
                        match_count = 0,
                        checked_at = NULL,
                        updated_at = SYSUTCDATETIME()
                    WHERE status = N'error'
                      AND last_error LIKE ?
                    """,
                    needle,
                )
            else:
                cur.execute(
                    """
                    UPDATE dbo.eh_sha_checks
                    SET status = N'pending',
                        last_error = NULL,
                        match_count = 0,
                        checked_at = NULL,
                        updated_at = SYSUTCDATETIME()
                    WHERE status = N'error'
                    """
                )
            n = int(cur.rowcount or 0)
            self._conn().commit()
            return n

    def is_queued(self, url: str) -> bool:
        key = gallery_key_from_url(url)
        if not key:
            return False
        return self.is_queued_key(key)

    def is_queued_key(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            row = self._conn().cursor().execute(
                "SELECT 1 FROM dbo.queue_items WHERE gallery_key = ?", key
            ).fetchone()
        return bool(row)

    def is_completed_key(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            row = self._conn().cursor().execute(
                "SELECT 1 FROM dbo.galleries WHERE gallery_key = ?", key
            ).fetchone()
        return bool(row)

    def find_gallery_by_key(self, key: str) -> dict | None:
        if not key:
            return None
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT gallery_key, token, url, title, out_dir, image_total
                FROM dbo.galleries
                WHERE gallery_key = ?
                """,
                key,
            ).fetchone()
        if not row:
            return None
        return {
            "gallery_key": row[0],
            "token": row[1],
            "url": row[2],
            "title": row[3],
            "out_dir": row[4],
            "image_total": row[5],
        }

    def find_queue_by_key(self, key: str) -> dict | None:
        """In-progress / queued gallery meta (title, out_dir) by gallery_key."""
        key = (key or "").strip()[:64]
        if not key:
            return None
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT gallery_key, url, title, out_dir, status
                FROM dbo.queue_items
                WHERE gallery_key = ?
                """,
                key,
            ).fetchone()
        if not row:
            return None
        return {
            "gallery_key": row[0],
            "url": row[1],
            "title": row[2],
            "out_dir": row[3],
            "status": row[4],
        }

    def resolve_gallery_meta(self, key: str) -> dict | None:
        """Completed galleries row, else queue_items (for still-running dupes)."""
        gal = self.find_gallery_by_key(key)
        if gal and (gal.get("out_dir") or gal.get("title")):
            return gal
        q = self.find_queue_by_key(key)
        if q:
            return q
        return gal or q

    def find_gallery_by_out_dir(self, out_dir: str) -> dict | None:
        """Match completed gallery by full folder path (case-insensitive on Windows)."""
        path = (out_dir or "").strip()
        if not path:
            return None
        leaf = Path(path).name
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                """
                SELECT TOP 1 gallery_key, token, url, title, out_dir, image_total
                FROM dbo.galleries
                WHERE out_dir = ?
                   OR LOWER(out_dir) = LOWER(?)
                ORDER BY id DESC
                """,
                path,
                path,
            ).fetchone()
            if not row and leaf:
                row = cur.execute(
                    """
                    SELECT TOP 1 gallery_key, token, url, title, out_dir, image_total
                    FROM dbo.galleries
                    WHERE title = ?
                    ORDER BY id DESC
                    """,
                    leaf,
                ).fetchone()
        if not row:
            return None
        return {
            "gallery_key": row[0],
            "token": row[1],
            "url": row[2],
            "title": row[3],
            "out_dir": row[4],
            "image_total": row[5],
        }

    def find_queue_by_out_dir(self, out_dir: str) -> dict | None:
        path = (out_dir or "").strip()
        if not path:
            return None
        leaf = Path(path).name
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT TOP 1 gallery_key, url, title, out_dir, status
                FROM dbo.queue_items
                WHERE out_dir = ?
                   OR LOWER(out_dir) = LOWER(?)
                   OR title = ?
                ORDER BY id DESC
                """,
                path,
                path,
                leaf,
            ).fetchone()
        if not row:
            return None
        return {
            "gallery_key": row[0],
            "url": row[1],
            "title": row[2],
            "out_dir": row[3],
            "status": row[4],
        }

    def local_folder_status(self, folder: str | Path) -> dict:
        """DB / queue status for a local gallery folder path."""
        folder = Path(folder)
        path = str(folder)
        gal = self.find_gallery_by_out_dir(path)
        q = self.find_queue_by_out_dir(path)
        return {
            "path": path,
            "name": folder.name,
            "in_galleries": bool(gal),
            "in_queue": bool(q),
            "gallery": gal,
            "queue": q,
            "gallery_key": (gal or q or {}).get("gallery_key"),
            "url": (gal or q or {}).get("url"),
            "title": (gal or q or {}).get("title"),
        }

    def remove_by_url(self, url: str) -> None:
        key = gallery_key_from_url(url)
        if not key:
            return
        with self._lock:
            self._conn().cursor().execute(
                "DELETE FROM dbo.queue_items WHERE gallery_key = ?", key
            )
            self._conn().commit()

    def clear_queue(self) -> None:
        with self._lock:
            cur = self._conn().cursor()
            cur.execute("DELETE FROM dbo.queue_images")
            cur.execute("DELETE FROM dbo.queue_items")
            self._conn().commit()

    def resequence(self, urls: list[str]) -> None:
        """Set position from current UI order; drop items no longer listed."""
        with self._lock:
            cur = self._conn().cursor()
            keys = []
            for i, url in enumerate(urls):
                key = gallery_key_from_url(url)
                if not key:
                    continue
                keys.append(key)
                cur.execute(
                    """
                    UPDATE dbo.queue_items
                    SET position = ?, url = ?, updated_at = SYSUTCDATETIME()
                    WHERE gallery_key = ?
                    """,
                    i,
                    url.strip(),
                    key,
                )
            if keys:
                placeholders = ",".join("?" * len(keys))
                cur.execute(
                    f"DELETE FROM dbo.queue_items WHERE gallery_key NOT IN ({placeholders})",
                    keys,
                )
            else:
                cur.execute("DELETE FROM dbo.queue_items")
            self._conn().commit()

    def mark_running(self, url: str, out_dir: str | None = None) -> int | None:
        key = gallery_key_from_url(url)
        if not key:
            return None
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                UPDATE dbo.queue_items
                SET status = N'running',
                    out_dir = COALESCE(?, out_dir),
                    last_error = NULL,
                    updated_at = SYSUTCDATETIME()
                WHERE gallery_key = ?
                """,
                out_dir,
                key,
            )
            row = cur.execute(
                "SELECT id FROM dbo.queue_items WHERE gallery_key = ?", key
            ).fetchone()
            self._conn().commit()
            return int(row[0]) if row else None

    def mark_stopped(self, url: str) -> None:
        key = gallery_key_from_url(url)
        if not key:
            return
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.queue_items
                SET status = N'stopped', updated_at = SYSUTCDATETIME()
                WHERE gallery_key = ? AND status = N'running'
                """,
                key,
            )
            self._conn().commit()

    def mark_failed(self, url: str, error: str) -> None:
        key = gallery_key_from_url(url)
        if not key:
            return
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.queue_items
                SET status = N'failed',
                    last_error = ?,
                    updated_at = SYSUTCDATETIME()
                WHERE gallery_key = ?
                """,
                (error or "")[:1000],
                key,
            )
            self._conn().commit()

    def set_gallery_meta(
        self,
        url: str,
        *,
        title: str | None = None,
        out_dir: str | None = None,
        image_total: int | None = None,
    ) -> None:
        key = gallery_key_from_url(url)
        if not key:
            return
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.queue_items
                SET title = COALESCE(?, title),
                    out_dir = COALESCE(?, out_dir),
                    image_total = COALESCE(?, image_total),
                    updated_at = SYSUTCDATETIME()
                WHERE gallery_key = ?
                """,
                title,
                out_dir,
                image_total,
                key,
            )
            self._conn().commit()

    def replace_images(self, url: str, links: list[tuple[str, str]]) -> int | None:
        """Replace per-image temp rows. ``links`` must already be normalized
        (ordered unique filenames from :func:`normalize_image_links`).
        """
        key = gallery_key_from_url(url)
        if not key:
            return None
        with self._lock:
            conn = self._conn()
            cur = conn.cursor()
            try:
                row = cur.execute(
                    "SELECT id FROM dbo.queue_items WHERE gallery_key = ?", key
                ).fetchone()
                if not row:
                    return None
                qid = int(row[0])
                cur.execute("DELETE FROM dbo.queue_images WHERE queue_id = ?", qid)
                inserted = 0
                seen: set[str] = set()
                for page_url, filename in links:
                    page_url = (page_url or "").strip()
                    filename = (filename or "").strip()
                    if not page_url or not filename:
                        continue
                    pid = image_page_id(page_url) or page_url
                    if pid in seen:
                        continue
                    seen.add(pid)
                    cur.execute(
                        """
                        INSERT INTO dbo.queue_images
                            (queue_id, page_url, filename, status)
                        VALUES (?, ?, ?, N'pending')
                        """,
                        qid,
                        page_url[:512],
                        filename[:260],
                    )
                    inserted += 1
                cur.execute(
                    """
                    UPDATE dbo.queue_items
                    SET image_total = ?, saved = 0, skipped = 0, failed = 0,
                        updated_at = SYSUTCDATETIME()
                    WHERE id = ?
                    """,
                    inserted,
                    qid,
                )
                conn.commit()
                return qid
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def update_image(
        self,
        url: str,
        filename: str,
        status: str,
        *,
        error: str | None = None,
        bump: str | None = None,
    ) -> None:
        """Update one image row; optional bump is 'saved'|'skipped'|'failed'."""
        key = gallery_key_from_url(url)
        if not key:
            return
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                "SELECT id FROM dbo.queue_items WHERE gallery_key = ?", key
            ).fetchone()
            if not row:
                return
            qid = int(row[0])
            cur.execute(
                """
                UPDATE dbo.queue_images
                SET status = ?,
                    last_error = ?,
                    updated_at = SYSUTCDATETIME()
                WHERE queue_id = ? AND filename = ?
                """,
                status,
                (error or "")[:1000] if error else None,
                qid,
                filename,
            )
            if bump in ("saved", "skipped", "failed"):
                cur.execute(
                    f"""
                    UPDATE dbo.queue_items
                    SET [{bump}] = [{bump}] + 1,
                        updated_at = SYSUTCDATETIME()
                    WHERE id = ?
                    """,
                    qid,
                )
            self._conn().commit()

    def complete_gallery(
        self,
        url: str,
        *,
        title: str | None,
        out_dir: str | None,
        image_total: int | None,
        saved: int,
        skipped: int,
        failed: int,
    ) -> None:
        """Write permanent dedupe row and purge all temp queue data for this gallery."""
        key = gallery_key_from_url(url)
        if not key:
            return
        token = gallery_token_from_url(url)
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                MERGE dbo.galleries AS t
                USING (
                    SELECT
                        ? AS gallery_key,
                        ? AS token,
                        ? AS url,
                        ? AS title,
                        ? AS out_dir,
                        ? AS image_total,
                        ? AS saved,
                        ? AS skipped,
                        ? AS failed
                ) AS s
                ON t.gallery_key = s.gallery_key
                WHEN MATCHED THEN UPDATE SET
                    token = s.token,
                    url = s.url,
                    title = COALESCE(s.title, t.title),
                    out_dir = COALESCE(s.out_dir, t.out_dir),
                    image_total = s.image_total,
                    saved = s.saved,
                    skipped = s.skipped,
                    failed = s.failed,
                    completed_at = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN INSERT
                    (gallery_key, token, url, title, out_dir,
                     image_total, saved, skipped, failed)
                VALUES
                    (s.gallery_key, s.token, s.url, s.title, s.out_dir,
                     s.image_total, s.saved, s.skipped, s.failed);
                """,
                key,
                token,
                url.strip(),
                title,
                out_dir,
                image_total,
                saved,
                skipped,
                failed,
            )
            # CASCADE deletes queue_images
            cur.execute("DELETE FROM dbo.queue_items WHERE gallery_key = ?", key)
            self._conn().commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
