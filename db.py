"""MSSQL persistence for EH gallery queue.

Uses a dedicated ``EH`` database on the same SQL Server as WishAssistance.
In-progress queue + per-image rows are temporary; successful galleries leave
only a top-level ``galleries`` row for dedupe.
"""

from __future__ import annotations

import configparser
import re
import threading
import uuid
from pathlib import Path

import pyodbc

from image_dhash import from_sql_bigint, order_sha_pair, to_sql_bigint

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

                -- Unreadable sample (missing/corrupt): stop fill spin-loop.
                IF COL_LENGTH(N'dbo.image_fingerprints', N'dhash_failed') IS NULL
                ALTER TABLE dbo.image_fingerprints ADD
                    dhash_failed BIT NOT NULL
                        CONSTRAINT DF_fp_dhash_failed DEFAULT (0);

                -- dHash near-dupe pairs (cross-gallery, sha1_a < sha1_b).
                IF OBJECT_ID(N'dbo.dhash_near_pairs', N'U') IS NULL
                CREATE TABLE dbo.dhash_near_pairs (
                    sha1_a       BINARY(20)  NOT NULL,
                    sha1_b       BINARY(20)  NOT NULL,
                    hamming      TINYINT     NOT NULL,
                    updated_at   DATETIME2   NOT NULL
                        CONSTRAINT DF_dhash_near_upd DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT PK_dhash_near_pairs PRIMARY KEY (sha1_a, sha1_b),
                    CONSTRAINT CK_dhash_near_order CHECK (sha1_a < sha1_b)
                );

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_dhash_near_b'
                      AND object_id = OBJECT_ID(N'dbo.dhash_near_pairs')
                )
                CREATE INDEX IX_dhash_near_b
                    ON dbo.dhash_near_pairs (sha1_b);

                -- User-dismissed near matches (never re-surface).
                IF OBJECT_ID(N'dbo.dhash_false_positives', N'U') IS NULL
                CREATE TABLE dbo.dhash_false_positives (
                    sha1_a       BINARY(20)  NOT NULL,
                    sha1_b       BINARY(20)  NOT NULL,
                    created_at   DATETIME2   NOT NULL
                        CONSTRAINT DF_dhash_fp_created DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT PK_dhash_false_positives PRIMARY KEY (sha1_a, sha1_b),
                    CONSTRAINT CK_dhash_fp_order CHECK (sha1_a < sha1_b)
                );

                -- dhash | manual (neighbor Link). Rebuild must not wipe manuals.
                IF COL_LENGTH(N'dbo.dhash_near_pairs', N'source') IS NULL
                ALTER TABLE dbo.dhash_near_pairs ADD
                    source NVARCHAR(16) NOT NULL
                        CONSTRAINT DF_dhash_near_src DEFAULT (N'dhash');

                -- User-confirmed SHA identity groups (Duped compare Done).
                -- Members behave like one content id for shared/dupe listing + skip.
                IF OBJECT_ID(N'dbo.sha1_match_groups', N'U') IS NULL
                CREATE TABLE dbo.sha1_match_groups (
                    group_id     UNIQUEIDENTIFIER NOT NULL,
                    sha1         BINARY(20)       NOT NULL,
                    created_at   DATETIME2        NOT NULL
                        CONSTRAINT DF_sha1_match_created
                        DEFAULT (SYSUTCDATETIME()),
                    CONSTRAINT PK_sha1_match_groups PRIMARY KEY (sha1)
                );

                IF NOT EXISTS (
                    SELECT 1 FROM sys.indexes
                    WHERE name = N'IX_sha1_match_group'
                      AND object_id = OBJECT_ID(N'dbo.sha1_match_groups')
                )
                CREATE INDEX IX_sha1_match_group
                    ON dbo.sha1_match_groups (group_id);
                """
            )
            self._conn().commit()

    def lookup_sha1(self, digest: bytes) -> dict | None:
        """Return fingerprint row for digest, or an equivalent match-group member."""
        if not digest or len(digest) != 20:
            return None
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                """
                SELECT sample_path, byte_len, gallery_key, seen_count, dhash, sha1
                FROM dbo.image_fingerprints
                WHERE sha1 = ?
                """,
                digest,
            ).fetchone()
            if not row:
                # Prefer any equivalent that already has a sample path (home).
                row = cur.execute(
                    """
                    SELECT TOP (1)
                        f.sample_path, f.byte_len, f.gallery_key, f.seen_count,
                        f.dhash, f.sha1
                    FROM dbo.sha1_match_groups g0
                    INNER JOIN dbo.sha1_match_groups g1
                        ON g1.group_id = g0.group_id
                    INNER JOIN dbo.image_fingerprints f ON f.sha1 = g1.sha1
                    WHERE g0.sha1 = ?
                      AND f.sample_path IS NOT NULL
                      AND LTRIM(RTRIM(f.sample_path)) <> N''
                    ORDER BY ISNULL(f.home_decided, 0) DESC, f.seen_count DESC
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
            "dhash": from_sql_bigint(int(row[4])) if row[4] is not None else None,
            "sha1": bytes(row[5]) if row[5] is not None else digest,
            "matched_via": "exact" if bytes(row[5]) == digest else "equivalent",
        }

    def equivalent_shas(self, digest: bytes) -> set[bytes]:
        """All SHAs in the same match group as ``digest`` (includes self)."""
        if not digest or len(digest) != 20:
            return set()
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT g1.sha1
                FROM dbo.sha1_match_groups g0
                INNER JOIN dbo.sha1_match_groups g1
                    ON g1.group_id = g0.group_id
                WHERE g0.sha1 = ?
                """,
                digest,
            ).fetchall()
        if not rows:
            return {digest}
        return {bytes(r[0]) for r in rows} | {digest}

    def merge_sha1_match(self, sha_a: bytes, sha_b: bytes) -> bool:
        """Union two digests into one match group (alias-like identity)."""
        if not sha_a or not sha_b or len(sha_a) != 20 or len(sha_b) != 20:
            return False
        if sha_a == sha_b:
            return False

        with self._lock:
            cur = self._conn().cursor()
            ga = cur.execute(
                "SELECT group_id FROM dbo.sha1_match_groups WHERE sha1 = ?",
                sha_a,
            ).fetchone()
            gb = cur.execute(
                "SELECT group_id FROM dbo.sha1_match_groups WHERE sha1 = ?",
                sha_b,
            ).fetchone()
            if ga and gb:
                id_a, id_b = ga[0], gb[0]
                if id_a == id_b:
                    self._conn().commit()
                    return False
                cur.execute(
                    """
                    UPDATE dbo.sha1_match_groups
                    SET group_id = ?
                    WHERE group_id = ?
                    """,
                    id_a,
                    id_b,
                )
            elif ga:
                cur.execute(
                    """
                    INSERT INTO dbo.sha1_match_groups (group_id, sha1)
                    VALUES (?, ?)
                    """,
                    ga[0],
                    sha_b,
                )
            elif gb:
                cur.execute(
                    """
                    INSERT INTO dbo.sha1_match_groups (group_id, sha1)
                    VALUES (?, ?)
                    """,
                    gb[0],
                    sha_a,
                )
            else:
                gid = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO dbo.sha1_match_groups (group_id, sha1)
                    VALUES (?, ?)
                    """,
                    gid,
                    sha_a,
                )
                cur.execute(
                    """
                    INSERT INTO dbo.sha1_match_groups (group_id, sha1)
                    VALUES (?, ?)
                    """,
                    gid,
                    sha_b,
                )
            self._conn().commit()
        return True

    def merge_sha1_matches(self, pairs: list[tuple[bytes, bytes]]) -> int:
        """Merge many Same pairs; returns number of successful merges."""
        n = 0
        for a, b in pairs:
            try:
                if self.merge_sha1_match(a, b):
                    n += 1
            except Exception:
                raise
        return n

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
        sql_dhash = to_sql_bigint(dhash) if dhash is not None else None
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
                    sql_dhash,
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
                    sql_dhash,
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
                    dhash_failed = 0,
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

        Also includes galleries linked only via ``sha1_match_groups`` (Done).
        """
        having_sql = ""
        if undecided_only:
            having_sql = (
                "HAVING COUNT(DISTINCT CASE "
                "WHEN ISNULL(f.home_decided, 0) = 0 THEN sp.sha1 END) > 0"
            )
        with self._lock:
            cur = self._conn().cursor()
            has_groups = bool(
                cur.execute(
                    "SELECT TOP (1) 1 FROM dbo.sha1_match_groups"
                ).fetchone()
            )
            # Exact same-sha shares (indexed). Optional UNION for match groups —
            # never OR them into one join (that nests the full aliases table).
            if has_groups:
                share_cte = """
                WITH share_pairs AS (
                    SELECT a.gallery_key, a.sha1, peer.gallery_key AS peer_gk
                    FROM dbo.image_name_aliases a
                    INNER JOIN dbo.image_name_aliases peer
                        ON peer.sha1 = a.sha1
                       AND peer.gallery_key <> a.gallery_key
                       AND peer.gallery_key <> N''
                    WHERE a.gallery_key <> N''
                    UNION
                    SELECT a.gallery_key, a.sha1, peer.gallery_key
                    FROM dbo.image_name_aliases a
                    INNER JOIN dbo.sha1_match_groups ga ON ga.sha1 = a.sha1
                    INNER JOIN dbo.sha1_match_groups gb
                        ON gb.group_id = ga.group_id
                       AND gb.sha1 <> ga.sha1
                    INNER JOIN dbo.image_name_aliases peer
                        ON peer.sha1 = gb.sha1
                       AND peer.gallery_key <> a.gallery_key
                       AND peer.gallery_key <> N''
                    WHERE a.gallery_key <> N''
                )
                """
                from_sql = """
                FROM share_pairs sp
                INNER JOIN dbo.image_fingerprints f ON f.sha1 = sp.sha1
                LEFT JOIN dbo.galleries g ON g.gallery_key = sp.gallery_key
                LEFT JOIN dbo.queue_items q ON q.gallery_key = sp.gallery_key
                """
                select_key = "sp.gallery_key"
                group_key = "sp.gallery_key"
                peer_col = "sp.peer_gk"
                sha_col = "sp.sha1"
            else:
                share_cte = ""
                from_sql = """
                FROM dbo.image_name_aliases a
                INNER JOIN dbo.image_name_aliases peer
                    ON peer.sha1 = a.sha1
                   AND peer.gallery_key <> a.gallery_key
                   AND peer.gallery_key <> N''
                INNER JOIN dbo.image_fingerprints f ON f.sha1 = a.sha1
                LEFT JOIN dbo.galleries g ON g.gallery_key = a.gallery_key
                LEFT JOIN dbo.queue_items q ON q.gallery_key = a.gallery_key
                WHERE a.gallery_key <> N''
                """
                select_key = "a.gallery_key"
                group_key = "a.gallery_key"
                peer_col = "peer.gallery_key"
                sha_col = "a.sha1"
                if undecided_only:
                    having_sql = (
                        "HAVING COUNT(DISTINCT CASE "
                        "WHEN ISNULL(f.home_decided, 0) = 0 THEN a.sha1 END) > 0"
                    )
            rows = cur.execute(
                f"""
                {share_cte}
                SELECT TOP (?)
                    {select_key},
                    COUNT(DISTINCT {sha_col}) AS shared_count,
                    COUNT(DISTINCT {peer_col}) AS peer_count,
                    MAX(COALESCE(g.title, q.title)) AS title,
                    MAX(COALESCE(g.out_dir, q.out_dir)) AS out_dir,
                    MAX(COALESCE(g.url, q.url)) AS url,
                    COUNT(DISTINCT CASE
                        WHEN ISNULL(f.home_decided, 0) = 0 THEN {sha_col} END
                    ) AS undecided_count
                {from_sql}
                GROUP BY {group_key}
                {having_sql}
                ORDER BY
                    COUNT(DISTINCT CASE
                        WHEN ISNULL(f.home_decided, 0) = 0 THEN {sha_col} END
                    ) DESC,
                    COUNT(DISTINCT {sha_col}) DESC,
                    {select_key} ASC
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
        """SHA-1s for ``gallery_key`` that also appear under other galleries.

        Includes peers linked only via ``sha1_match_groups`` (compare Done).
        Aliases from equivalent digests are merged onto each row so Duped can
        resolve peer names/paths.
        """
        key = (gallery_key or "").strip()[:64]
        if not key:
            return []
        undecided_sql = ""
        if undecided_only:
            undecided_sql = "AND ISNULL(f.home_decided, 0) = 0"
        with self._lock:
            cur = self._conn().cursor()
            has_groups = bool(
                cur.execute(
                    "SELECT TOP (1) 1 FROM dbo.sha1_match_groups"
                ).fetchone()
            )
            # Fast path: same sha1 in another gallery.
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
            by_digest: dict[bytes, tuple] = {
                bytes(r[0]): r for r in sha_rows
            }
            if has_groups:
                # Match-group peers only (indexed via sha1 PK on groups).
                group_rows = cur.execute(
                    f"""
                    SELECT DISTINCT a.sha1, f.sample_path, f.gallery_key,
                           f.byte_len, f.seen_count, ISNULL(f.home_decided, 0),
                           gb.sha1
                    FROM dbo.image_name_aliases a
                    INNER JOIN dbo.image_fingerprints f ON f.sha1 = a.sha1
                    INNER JOIN dbo.sha1_match_groups ga ON ga.sha1 = a.sha1
                    INNER JOIN dbo.sha1_match_groups gb
                        ON gb.group_id = ga.group_id
                       AND gb.sha1 <> ga.sha1
                    INNER JOIN dbo.image_name_aliases b
                        ON b.sha1 = gb.sha1
                       AND b.gallery_key <> a.gallery_key
                       AND b.gallery_key <> N''
                    WHERE a.gallery_key = ?
                      {undecided_sql}
                    """,
                    key,
                ).fetchall()
            else:
                group_rows = []
            peer_for: dict[bytes, bytes] = {}
            for r in group_rows:
                digest = bytes(r[0])
                peer_sha = bytes(r[6])
                peer_for[digest] = peer_sha
                if digest not in by_digest:
                    by_digest[digest] = r[:6]
            if not by_digest:
                return []
            digests = list(by_digest.keys())
            all_shas = set(digests)
            for p in peer_for.values():
                all_shas.add(p)
            # Also pull any other group members for alias merge.
            if has_groups and digests:
                placeholders = ",".join("?" * len(digests))
                for r in cur.execute(
                    f"""
                    SELECT DISTINCT g1.sha1
                    FROM dbo.sha1_match_groups g0
                    INNER JOIN dbo.sha1_match_groups g1
                        ON g1.group_id = g0.group_id
                    WHERE g0.sha1 IN ({placeholders})
                    """,
                    *digests,
                ).fetchall():
                    all_shas.add(bytes(r[0]))
            all_list = list(all_shas)
            ph2 = ",".join("?" * len(all_list))
            alias_rows = cur.execute(
                f"""
                SELECT sha1, name, bare_name, gallery_key, sample_path
                FROM dbo.image_name_aliases
                WHERE sha1 IN ({ph2})
                ORDER BY created_at ASC, id ASC
                """,
                *all_list,
            ).fetchall()
        by_sha: dict[bytes, list[dict]] = {d: [] for d in all_list}
        for r in alias_rows:
            digest = bytes(r[0])
            by_sha.setdefault(digest, []).append(
                {
                    "name": r[1],
                    "bare_name": r[2],
                    "gallery_key": r[3] or "",
                    "sample_path": r[4],
                    "sha1": digest,
                }
            )
        out = []
        for digest, r in by_digest.items():
            peer_shas = {digest}
            if digest in peer_for:
                peer_shas.add(peer_for[digest])
            # Include all group members we loaded for this digest.
            for eq in all_shas:
                if eq == digest or peer_for.get(digest) == eq:
                    peer_shas.add(eq)
            merged: list[dict] = []
            seen_alias: set[tuple] = set()
            for eq in peer_shas:
                for a in by_sha.get(eq, []):
                    sig = (a.get("gallery_key"), a.get("name"), eq)
                    if sig in seen_alias:
                        continue
                    # Keep aliases for this digest + peer galleries only when
                    # expanding via match group; same-sha aliases always keep.
                    if eq == digest or eq == peer_for.get(digest):
                        seen_alias.add(sig)
                        merged.append(a)
                    elif a.get("gallery_key") and a.get("gallery_key") != key:
                        # Other group member names from peer galleries.
                        seen_alias.add(sig)
                        merged.append(a)
            # For exact-only rows, still need peer aliases on same sha.
            if digest not in peer_for:
                merged = []
                seen_alias = set()
                for a in by_sha.get(digest, []):
                    sig = (a.get("gallery_key"), a.get("name"), digest)
                    if sig in seen_alias:
                        continue
                    seen_alias.add(sig)
                    merged.append(a)
            peer_sha = peer_for.get(digest)
            out.append(
                {
                    "sha1": digest,
                    "sha1_hex": digest.hex(),
                    "sample_path": r[1],
                    "home_gallery_key": r[2],
                    "byte_len": int(r[3]) if r[3] is not None else None,
                    "seen_count": int(r[4] or 0),
                    "home_decided": bool(r[5]),
                    "peer_sha1": peer_sha if peer_sha and peer_sha != digest else None,
                    "aliases": merged,
                }
            )
        out.sort(key=lambda x: x["sha1_hex"])
        return out

    # --- dHash near-dupes -------------------------------------------------

    def dhash_fill_stats(self) -> dict:
        """Counts for fill progress (total / filled / missing / failed)."""
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN dhash IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE
                        WHEN dhash IS NULL AND ISNULL(dhash_failed, 0) = 0
                        THEN 1 ELSE 0 END),
                    SUM(CASE WHEN ISNULL(dhash_failed, 0) = 1 THEN 1 ELSE 0 END)
                FROM dbo.image_fingerprints
                """
            ).fetchone()
        return {
            "total": int(row[0] or 0),
            "filled": int(row[1] or 0),
            "missing": int(row[2] or 0),
            "failed": int(row[3] or 0),
        }

    def list_fingerprints_missing_dhash(self, *, limit: int = 100) -> list[dict]:
        """Batch of fingerprints that still need a dHash (have sample_path)."""
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT TOP (?) sha1, sample_path
                FROM dbo.image_fingerprints
                WHERE dhash IS NULL
                  AND ISNULL(dhash_failed, 0) = 0
                  AND sample_path IS NOT NULL
                  AND sample_path <> N''
                ORDER BY updated_at ASC
                """,
                int(limit),
            ).fetchall()
        return [
            {"sha1": bytes(r[0]), "sample_path": r[1]} for r in rows
        ]

    def list_paths_for_sha(self, digest: bytes) -> list[str]:
        """Canonical + alias sample paths for a SHA (deduped, non-empty)."""
        if not digest or len(digest) != 20:
            return []
        with self._lock:
            cur = self._conn().cursor()
            row = cur.execute(
                """
                SELECT sample_path FROM dbo.image_fingerprints WHERE sha1 = ?
                """,
                digest,
            ).fetchone()
            alias_rows = cur.execute(
                """
                SELECT DISTINCT sample_path
                FROM dbo.image_name_aliases
                WHERE sha1 = ?
                  AND sample_path IS NOT NULL
                  AND sample_path <> N''
                """,
                digest,
            ).fetchall()
        seen: set[str] = set()
        out: list[str] = []
        for raw in ([row[0] if row else None] + [r[0] for r in alias_rows]):
            p = (raw or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            out.append(p)
        return out

    def touch_fingerprint(self, digest: bytes) -> None:
        """Bump ``updated_at`` so failed dHash fills rotate to the back."""
        if not digest or len(digest) != 20:
            return
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.image_fingerprints
                SET updated_at = SYSUTCDATETIME()
                WHERE sha1 = ?
                """,
                digest,
            )
            self._conn().commit()

    def mark_dhash_failed(self, digest: bytes) -> None:
        """Stop retrying unreadable samples (missing/corrupt file)."""
        if not digest or len(digest) != 20:
            return
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.image_fingerprints
                SET dhash_failed = 1, updated_at = SYSUTCDATETIME()
                WHERE sha1 = ? AND dhash IS NULL
                """,
                digest,
            )
            self._conn().commit()

    def set_fingerprint_dhash(self, digest: bytes, dhash: int) -> None:
        """Store unsigned 64-bit dHash for ``digest``."""
        if not digest or len(digest) != 20:
            raise ValueError("sha1 digest must be 20 bytes")
        with self._lock:
            self._conn().cursor().execute(
                """
                UPDATE dbo.image_fingerprints
                SET dhash = ?,
                    dhash_failed = 0,
                    updated_at = SYSUTCDATETIME()
                WHERE sha1 = ?
                """,
                to_sql_bigint(dhash),
                digest,
            )
            self._conn().commit()

    def load_dhash_rows(self) -> list[tuple[bytes, int]]:
        """All ``(sha1, dhash)`` with a filled dHash."""
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT sha1, dhash
                FROM dbo.image_fingerprints
                WHERE dhash IS NOT NULL
                """
            ).fetchall()
        out: list[tuple[bytes, int]] = []
        for r in rows:
            dh = from_sql_bigint(int(r[1]))
            if dh is not None:
                out.append((bytes(r[0]), dh))
        return out

    def load_sha_gallery_keys(self) -> dict[bytes, set[str]]:
        """sha1 → set of non-empty gallery keys from aliases."""
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT DISTINCT sha1, gallery_key
                FROM dbo.image_name_aliases
                WHERE gallery_key <> N''
                """
            ).fetchall()
        out: dict[bytes, set[str]] = {}
        for r in rows:
            digest = bytes(r[0])
            out.setdefault(digest, set()).add(r[1])
        return out

    def load_false_positive_pairs(self) -> set[tuple[bytes, bytes]]:
        with self._lock:
            rows = self._conn().cursor().execute(
                "SELECT sha1_a, sha1_b FROM dbo.dhash_false_positives"
            ).fetchall()
        return {(bytes(r[0]), bytes(r[1])) for r in rows}

    def rebuild_dhash_near_pairs(self, *, max_hamming: int = 5) -> dict:
        """Full in-memory BK-tree rebuild of ``dhash_near_pairs``.

        Skips false positives and same-gallery-only pairs. Returns stats.
        """
        from image_dhash import find_near_pairs

        rows = self.load_dhash_rows()
        gallery_keys = self.load_sha_gallery_keys()
        fps = self.load_false_positive_pairs()
        pairs = []
        for a, b, dist in find_near_pairs(
            rows, max_hamming=int(max_hamming), gallery_keys=gallery_keys
        ):
            if (a, b) in fps:
                continue
            pairs.append((a, b, int(dist)))

        with self._lock:
            cur = self._conn().cursor()
            # Keep user manual Links; only refresh automatic dHash pairs.
            cur.execute(
                """
                DELETE FROM dbo.dhash_near_pairs
                WHERE ISNULL(source, N'dhash') <> N'manual'
                """
            )
            if pairs:
                cur.fast_executemany = True
                cur.executemany(
                    """
                    INSERT INTO dbo.dhash_near_pairs (sha1_a, sha1_b, hamming, source)
                    VALUES (?, ?, ?, N'dhash')
                    """,
                    pairs,
                )
            self._conn().commit()
        return {
            "fingerprints": len(rows),
            "pairs": len(pairs),
            "max_hamming": int(max_hamming),
        }

    def upsert_dhash_near_for_sha(
        self,
        digest: bytes,
        dhash: int,
        *,
        max_hamming: int = 5,
        tree_hits: list[tuple[bytes, int]] | None = None,
        gallery_keys: dict[bytes, set[str]] | None = None,
        false_positives: set[tuple[bytes, bytes]] | None = None,
    ) -> int:
        """Insert near pairs involving ``digest`` (from BK-tree probe hits).

        ``tree_hits`` is ``[(other_sha, hamming), ...]``. Returns insert count.
        Pass ``gallery_keys`` / ``false_positives`` from the worker to avoid
        reloading the full maps on every fingerprint.
        """
        if not digest or len(digest) != 20:
            return 0
        if gallery_keys is None:
            gallery_keys = self.load_sha_gallery_keys()
        if false_positives is None:
            false_positives = self.load_false_positive_pairs()
        ga = gallery_keys.get(digest) or set()
        fps = false_positives
        rows = []
        for other, dist in tree_hits or []:
            if not other or other == digest or dist > max_hamming:
                continue
            a, b = order_sha_pair(digest, other)
            if (a, b) in fps:
                continue
            gb = gallery_keys.get(other) or set()
            if not ga or not gb or (ga == gb and len(ga) == 1):
                continue
            rows.append((a, b, int(dist)))
        if not rows:
            return 0
        with self._lock:
            cur = self._conn().cursor()
            for a, b, dist in rows:
                cur.execute(
                    """
                    MERGE dbo.dhash_near_pairs AS t
                    USING (SELECT ? AS sha1_a, ? AS sha1_b, ? AS hamming) AS s
                    ON t.sha1_a = s.sha1_a AND t.sha1_b = s.sha1_b
                    WHEN MATCHED AND ISNULL(t.source, N'dhash') = N'dhash' THEN
                        UPDATE SET hamming = s.hamming,
                                   updated_at = SYSUTCDATETIME()
                    WHEN NOT MATCHED THEN
                        INSERT (sha1_a, sha1_b, hamming, source)
                        VALUES (s.sha1_a, s.sha1_b, s.hamming, N'dhash');
                    """,
                    a,
                    b,
                    dist,
                )
            self._conn().commit()
        return len(rows)

    def add_manual_near_pair(self, sha_a: bytes, sha_b: bytes) -> bool:
        """Link two different SHAs by hand (neighbor match). Hamming=255 sentinel."""
        if not sha_a or not sha_b or sha_a == sha_b:
            return False
        a, b = order_sha_pair(sha_a, sha_b)
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                MERGE dbo.dhash_near_pairs AS t
                USING (SELECT ? AS sha1_a, ? AS sha1_b) AS s
                ON t.sha1_a = s.sha1_a AND t.sha1_b = s.sha1_b
                WHEN MATCHED THEN
                    UPDATE SET source = N'manual',
                               hamming = 255,
                               updated_at = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN
                    INSERT (sha1_a, sha1_b, hamming, source)
                    VALUES (s.sha1_a, s.sha1_b, 255, N'manual');
                """,
                a,
                b,
            )
            # Drop FP if user is explicitly linking.
            cur.execute(
                """
                DELETE FROM dbo.dhash_false_positives
                WHERE sha1_a = ? AND sha1_b = ?
                """,
                a,
                b,
            )
            self._conn().commit()
        return True

    def load_linked_sha_pairs(self) -> set[tuple[bytes, bytes]]:
        """All near/manual linked ordered pairs (for board neighbor hints)."""
        with self._lock:
            rows = self._conn().cursor().execute(
                "SELECT sha1_a, sha1_b FROM dbo.dhash_near_pairs"
            ).fetchall()
        return {(bytes(r[0]), bytes(r[1])) for r in rows}

    def shas_are_linked(self, sha_a: bytes, sha_b: bytes) -> bool:
        if not sha_a or not sha_b:
            return False
        if sha_a == sha_b:
            return True
        a, b = order_sha_pair(sha_a, sha_b)
        with self._lock:
            row = self._conn().cursor().execute(
                """
                SELECT 1 FROM dbo.dhash_near_pairs
                WHERE sha1_a = ? AND sha1_b = ?
                """,
                a,
                b,
            ).fetchone()
        return bool(row)

    def mark_dhash_false_positive(self, sha_a: bytes, sha_b: bytes) -> None:
        """Dismiss a near pair permanently and drop it from the cache."""
        a, b = order_sha_pair(sha_a, sha_b)
        with self._lock:
            cur = self._conn().cursor()
            cur.execute(
                """
                IF NOT EXISTS (
                    SELECT 1 FROM dbo.dhash_false_positives
                    WHERE sha1_a = ? AND sha1_b = ?
                )
                INSERT INTO dbo.dhash_false_positives (sha1_a, sha1_b)
                VALUES (?, ?)
                """,
                a,
                b,
                a,
                b,
            )
            cur.execute(
                """
                DELETE FROM dbo.dhash_near_pairs
                WHERE sha1_a = ? AND sha1_b = ?
                """,
                a,
                b,
            )
            self._conn().commit()

    def list_near_dupe_galleries(self, *, limit: int = 500) -> list[dict]:
        """Galleries that participate in ≥1 cached dHash near pair."""
        with self._lock:
            rows = self._conn().cursor().execute(
                """
                SELECT TOP (?)
                    a.gallery_key,
                    COUNT(DISTINCT CASE
                        WHEN p.sha1_a = a.sha1 THEN p.sha1_b ELSE p.sha1_a
                    END) AS shared_count,
                    COUNT(DISTINCT peer.gallery_key) AS peer_count,
                    MAX(COALESCE(g.title, q.title)) AS title,
                    MAX(COALESCE(g.out_dir, q.out_dir)) AS out_dir,
                    MAX(COALESCE(g.url, q.url)) AS url,
                    COUNT(DISTINCT CASE
                        WHEN p.sha1_a = a.sha1 THEN p.sha1_b ELSE p.sha1_a
                    END) AS undecided_count
                FROM dbo.dhash_near_pairs p
                INNER JOIN dbo.image_name_aliases a
                    ON a.sha1 IN (p.sha1_a, p.sha1_b)
                   AND a.gallery_key <> N''
                INNER JOIN dbo.image_name_aliases peer
                    ON peer.sha1 IN (p.sha1_a, p.sha1_b)
                   AND peer.gallery_key <> a.gallery_key
                   AND peer.gallery_key <> N''
                   AND (
                        (a.sha1 = p.sha1_a AND peer.sha1 = p.sha1_b)
                     OR (a.sha1 = p.sha1_b AND peer.sha1 = p.sha1_a)
                   )
                LEFT JOIN dbo.galleries g ON g.gallery_key = a.gallery_key
                LEFT JOIN dbo.queue_items q ON q.gallery_key = a.gallery_key
                GROUP BY a.gallery_key
                ORDER BY shared_count DESC, a.gallery_key ASC
                """,
                int(limit),
            ).fetchall()
        return [
            {
                "gallery_key": r[0],
                "shared_count": int(r[1] or 0),
                "peer_count": int(r[2] or 0),
                "title": r[3],
                "out_dir": r[4],
                "url": r[5],
                "undecided_count": int(r[6] or 0),
            }
            for r in rows
        ]

    def list_near_files_for_gallery(self, gallery_key: str) -> list[dict]:
        """Near-dupe rows for ``gallery_key`` (one row per local sha + best peer)."""
        key = (gallery_key or "").strip()[:64]
        if not key:
            return []
        with self._lock:
            cur = self._conn().cursor()
            pair_rows = cur.execute(
                """
                SELECT
                    CASE WHEN la.sha1 = p.sha1_a THEN p.sha1_a ELSE p.sha1_b END
                        AS local_sha,
                    CASE WHEN la.sha1 = p.sha1_a THEN p.sha1_b ELSE p.sha1_a END
                        AS peer_sha,
                    p.hamming,
                    fl.sample_path AS local_sample,
                    fp.sample_path AS peer_sample,
                    fl.byte_len AS local_bytes,
                    fp.byte_len AS peer_bytes,
                    fl.dhash AS local_dhash,
                    fp.dhash AS peer_dhash,
                    ISNULL(p.source, N'dhash') AS pair_source
                FROM dbo.dhash_near_pairs p
                INNER JOIN dbo.image_name_aliases la
                    ON la.sha1 IN (p.sha1_a, p.sha1_b)
                   AND la.gallery_key = ?
                INNER JOIN dbo.image_fingerprints fl
                    ON fl.sha1 = CASE
                        WHEN la.sha1 = p.sha1_a THEN p.sha1_a ELSE p.sha1_b END
                INNER JOIN dbo.image_fingerprints fp
                    ON fp.sha1 = CASE
                        WHEN la.sha1 = p.sha1_a THEN p.sha1_b ELSE p.sha1_a END
                WHERE NOT EXISTS (
                    SELECT 1 FROM dbo.dhash_false_positives f
                    WHERE f.sha1_a = p.sha1_a AND f.sha1_b = p.sha1_b
                )
                ORDER BY
                    CASE WHEN ISNULL(p.source, N'dhash') = N'manual' THEN 1 ELSE 0 END,
                    p.hamming ASC,
                    local_sha
                """,
                key,
            ).fetchall()
            if not pair_rows:
                return []

            # Deduplicate: keep lowest-hamming peer per local sha.
            best: dict[bytes, tuple] = {}
            for r in pair_rows:
                local = bytes(r[0])
                prev = best.get(local)
                if prev is None or int(r[2]) < int(prev[2]):
                    best[local] = r

            digests: list[bytes] = []
            for r in best.values():
                digests.append(bytes(r[0]))
                digests.append(bytes(r[1]))
            digests = list(dict.fromkeys(digests))
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
        for r in best.values():
            local = bytes(r[0])
            peer = bytes(r[1])
            peer_aliases = by_sha.get(peer, [])
            peer_key = ""
            for a in peer_aliases:
                gk = a.get("gallery_key") or ""
                if gk and gk != key:
                    peer_key = gk
                    break
            out.append(
                {
                    "sha1": local,
                    "sha1_hex": local.hex(),
                    "peer_sha1": peer,
                    "peer_sha1_hex": peer.hex(),
                    "hamming": int(r[2]),
                    "sample_path": r[3],
                    "peer_sample_path": r[4],
                    "byte_len": int(r[5]) if r[5] is not None else None,
                    "peer_byte_len": int(r[6]) if r[6] is not None else None,
                    "dhash": from_sql_bigint(int(r[7])) if r[7] is not None else None,
                    "peer_dhash": (
                        from_sql_bigint(int(r[8])) if r[8] is not None else None
                    ),
                    "pair_source": (r[9] or "dhash").strip().lower(),
                    "home_gallery_key": peer_key,
                    "home_decided": False,
                    "match_kind": (
                        "manual"
                        if (r[9] or "").strip().lower() == "manual"
                        else "near"
                    ),
                    "aliases": by_sha.get(local, []) + peer_aliases,
                }
            )
        out.sort(
            key=lambda x: (
                0 if x["match_kind"] == "near" else 1,
                x["hamming"],
                x["sha1_hex"],
            )
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
                SELECT name, bare_name, sample_path, sha1
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
                "sha1": bytes(r[3]) if r[3] is not None else None,
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
