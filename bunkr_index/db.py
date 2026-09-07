"""SQLite storage: schema, FTS5 (trigram) indexes and data access."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA_VERSION = 3

# --------------------------------------------------------------------------
# Connections / schema
# --------------------------------------------------------------------------


def connect(path=None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con

_FILES_FTS_COLUMNS = ("search_name", "storage", "slug")


def _files_fts_columns(con: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row["name"] for row in con.execute("PRAGMA table_info(files_fts)"))


def _create_files_fts(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE VIRTUAL TABLE files_fts USING fts5(
            search_name, storage, slug,
            content='files',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
    con.execute(
        """
        CREATE TRIGGER files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, search_name, storage, slug)
            VALUES (new.id, new.search_name, new.storage, new.slug);
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, search_name, storage, slug)
            VALUES ('delete', old.id, old.search_name, old.storage, old.slug);
        END
        """
    )
    con.execute(
        """
        CREATE TRIGGER files_au AFTER UPDATE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, search_name, storage, slug)
            VALUES ('delete', old.id, old.search_name, old.storage, old.slug);
            INSERT INTO files_fts(rowid, search_name, storage, slug)
            VALUES (new.id, new.search_name, new.storage, new.slug);
        END
        """
    )


def _upgrade_files_fts(con: sqlite3.Connection) -> None:
    if _files_fts_columns(con) == _FILES_FTS_COLUMNS:
        return
    with con:
        for trigger in ("files_ai", "files_ad", "files_au"):
            con.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        con.execute("DROP TABLE IF EXISTS files_fts")
        _create_files_fts(con)
        con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")

def _file_extension(name: object) -> str:
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    _, dot, suffix = base.rpartition(".")
    suffix = suffix.casefold()
    ok = dot and 1 <= len(suffix) <= 16 and suffix.isascii() and suffix.isalnum()
    return suffix if ok else ""


def _upgrade_files_extension(con: sqlite3.Connection) -> None:
    columns = {row["name"] for row in con.execute("PRAGMA table_info(files)")}
    if "extension" not in columns:
        with con:
            con.execute(
                "ALTER TABLE files ADD COLUMN extension TEXT NOT NULL DEFAULT ''"
            )
            con.create_function("file_extension", 1, _file_extension)
            con.execute(
                "UPDATE files SET extension = file_extension(search_name) "
                "WHERE extension = ''"
            )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_files_extension_uploaded_id
        ON files(extension, uploaded_at DESC, id DESC)
        """
    )


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS albums (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bunkr_id      TEXT NOT NULL UNIQUE,
            title         TEXT NOT NULL DEFAULT '',
            file_count    INTEGER NOT NULL DEFAULT 0,
            thumb         TEXT NOT NULL DEFAULT '',
            discovered_at TEXT NOT NULL,
            updated_at    TEXT,
            indexed_at    TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT NOT NULL DEFAULT '',
            dead          INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id    TEXT NOT NULL REFERENCES albums(bunkr_id) ON DELETE CASCADE,
            file_id     INTEGER NOT NULL,
            search_name TEXT NOT NULL,
            storage     TEXT NOT NULL DEFAULT '',
            slug        TEXT NOT NULL DEFAULT '',
            mime        TEXT NOT NULL DEFAULT '',
            media       TEXT NOT NULL DEFAULT '',
            extension   TEXT NOT NULL DEFAULT '',
            size        INTEGER NOT NULL DEFAULT 0,
            uploaded_at TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_files_album_file
            ON files(album_id, file_id);
        CREATE INDEX IF NOT EXISTS idx_files_album ON files(album_id);
        CREATE INDEX IF NOT EXISTS idx_files_search_nocase
            ON files(search_name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_files_uploaded_id
            ON files(uploaded_at DESC, id DESC);
        DROP INDEX IF EXISTS idx_files_search;
        DROP INDEX IF EXISTS idx_files_uploaded;

        CREATE VIRTUAL TABLE IF NOT EXISTS albums_fts USING fts5(
            title,
            content='albums',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE TRIGGER IF NOT EXISTS albums_ai AFTER INSERT ON albums BEGIN
            INSERT INTO albums_fts(rowid, title) VALUES (new.id, new.title);
        END;
        CREATE TRIGGER IF NOT EXISTS albums_ad AFTER DELETE ON albums BEGIN
            INSERT INTO albums_fts(albums_fts, rowid, title)
            VALUES ('delete', old.id, old.title);
        END;
        CREATE TRIGGER IF NOT EXISTS albums_au AFTER UPDATE ON albums BEGIN
            INSERT INTO albums_fts(albums_fts, rowid, title)
            VALUES ('delete', old.id, old.title);
            INSERT INTO albums_fts(rowid, title) VALUES (new.id, new.title);
        END;

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    _upgrade_files_extension(con)
    _upgrade_files_fts(con)
    con.execute(
        """
        INSERT INTO meta(key, value) VALUES('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    con.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Discovery layer (balbums.st album records)
# --------------------------------------------------------------------------


def upsert_discovered(con: sqlite3.Connection, albums: Iterable[dict]) -> int:
    """Insert/refresh discovered albums. Returns number of new ids."""
    ts = now_iso()
    rows = list(albums)
    if not rows:
        return 0
    ids = [r["bunkr_id"] for r in rows]
    marks = ",".join("?" * len(rows))
    known = {
        r["bunkr_id"]
        for r in con.execute(
            f"SELECT bunkr_id FROM albums WHERE bunkr_id IN ({marks})",
            ids,
        ).fetchall()
    }
    con.executemany(
        """
        INSERT INTO albums (bunkr_id, title, file_count, thumb, discovered_at, updated_at)
        VALUES (:bunkr_id, :title, :file_count, :thumb, :discovered_at, :discovered_at)
        ON CONFLICT(bunkr_id) DO UPDATE SET
            title = excluded.title,
            file_count = excluded.file_count,
            thumb = CASE WHEN excluded.thumb = '' THEN albums.thumb ELSE excluded.thumb END,
            updated_at = excluded.updated_at
        """,
        [
            {
                "bunkr_id": r["bunkr_id"],
                "title": r["title"],
                "file_count": int(r["file_count"] or 0),
                "thumb": r.get("thumb") or "",
                "discovered_at": ts,
            }
            for r in rows
        ],
    )
    con.commit()
    return sum(1 for r in rows if r["bunkr_id"] not in known)


# --------------------------------------------------------------------------
# Album metadata crawl layer
# --------------------------------------------------------------------------


def set_album_pending(con: sqlite3.Connection, bunkr_ids: Iterable[str]) -> list[str]:
    """Enqueue album ids for crawl (used by manual `add`). New rows are
    discovered 'now' so pending-for-crawl (ordered newest first) picks them
    immediately."""
    ts = now_iso()
    ids = [i for i in bunkr_ids if i]
    for i in ids:
        con.execute(
            """
            INSERT INTO albums (bunkr_id, title, discovered_at, updated_at)
            VALUES (?, '', ?, ?)
            ON CONFLICT(bunkr_id) DO UPDATE SET attempts = 0, dead = 0,
                last_error = '', indexed_at = NULL, updated_at = excluded.updated_at
            """,
            (i, ts, ts),
        )
    con.commit()
    return ids


# Shared queue predicate: never-indexed albums (then stale ones when a
# refresh window is given). Two `?` params: the stale-days sentinel and the
# days value. Single source for queue reads and queue counting.
_PENDING_WHERE = """
    dead = 0 AND attempts < 5
      AND (indexed_at IS NULL
           OR (indexed_at IS NOT NULL AND ? IS NOT NULL
               AND indexed_at < datetime('now', '-' || ? || ' days')))
"""


def pending_for_crawl(
    con: sqlite3.Connection, limit: int = 200, include_stale_days: int | None = None
) -> list[sqlite3.Row]:
    """Not-yet-indexed albums first (newest discovery first), then optionally
    albums whose index is older than `stale_days`."""
    q = f"""
        SELECT bunkr_id, title, attempts FROM albums
        WHERE {_PENDING_WHERE}
        ORDER BY CASE WHEN indexed_at IS NULL THEN 0 ELSE 1 END,
                 discovered_at DESC
        LIMIT ?
    """
    return con.execute(q, (include_stale_days, include_stale_days, limit)).fetchall()


def count_pending_for_crawl(
    con: sqlite3.Connection, include_stale_days: int | None = None
) -> int:
    """Exact size of the crawl queue — the same set pending_for_crawl drains."""
    return con.execute(
        f"SELECT COUNT(*) FROM albums WHERE {_PENDING_WHERE}",
        (include_stale_days, include_stale_days),
    ).fetchone()[0]


def replace_album_files(
    con: sqlite3.Connection,
    bunkr_id: str,
    title: str,
    thumb: str,
    files: list[dict],
) -> int:
    """Transactional replace of one album's metadata. Returns file count."""
    ts = now_iso()
    with con:
        con.execute(
            """
            INSERT INTO albums (bunkr_id, title, discovered_at, updated_at, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(bunkr_id) DO UPDATE SET
                title = excluded.title,
                file_count = ?,
                thumb = CASE WHEN excluded.thumb = '' THEN albums.thumb ELSE excluded.thumb END,
                indexed_at = excluded.indexed_at,
                updated_at = excluded.updated_at,
                attempts = 0,
                last_error = '',
                dead = 0
            """,
            (bunkr_id, title, ts, ts, ts, len(files)),
        )
        con.execute("DELETE FROM files WHERE album_id = ?", (bunkr_id,))
        if files:
            con.executemany(
                """
                INSERT INTO files
                    (album_id, file_id, search_name, storage, slug, mime, media,
                     extension, size, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bunkr_id,
                        f["file_id"],
                        f["search_name"],
                        f.get("storage", ""),
                        f.get("slug", ""),
                        f.get("mime", ""),
                        f.get("media", ""),
                        f.get("extension") or _file_extension(f["search_name"]),
                        int(f.get("size") or 0),
                        f.get("uploaded_at"),
                    )
                    for f in files
                ],
            )
    return len(files)


def mark_album_error(
    con: sqlite3.Connection, bunkr_id: str, error: str, dead: bool = False
) -> None:
    ts = now_iso()
    with con:
        if dead:
            # A dead album is not 'indexed now': leave indexed_at untouched
            # (a previous successful crawl timestamp is history, not state)
            # so dead rows never leak into `indexed_at IS NOT NULL` listings.
            con.execute(
                "UPDATE albums SET attempts = attempts + 1, last_error = ?,"
                " dead = 1, updated_at = ? WHERE bunkr_id = ?",
                (error[:500], ts, bunkr_id),
            )
        else:
            con.execute(
                "UPDATE albums SET attempts = attempts + 1, last_error = ?, updated_at = ?"
                " WHERE bunkr_id = ?",
                (error[:500], ts, bunkr_id),
            )


def album_ids_for_refresh(con: sqlite3.Connection, limit: int) -> list[str]:
    rows = con.execute(
        "SELECT bunkr_id FROM albums WHERE dead = 0 ORDER BY indexed_at IS NULL DESC,"
        " discovered_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["bunkr_id"] for r in rows]


# --------------------------------------------------------------------------
# Read / stats
# --------------------------------------------------------------------------


def stats(con: sqlite3.Connection) -> dict:
    (total,) = con.execute("SELECT COUNT(*) FROM albums").fetchone()
    (indexed,) = con.execute(
        "SELECT COUNT(*) FROM albums WHERE indexed_at IS NOT NULL AND dead = 0"
    ).fetchone()
    (dead,) = con.execute("SELECT COUNT(*) FROM albums WHERE dead = 1").fetchone()
    (files,) = con.execute("SELECT COUNT(*) FROM files").fetchone()
    (last_idx,) = con.execute(
        "SELECT MAX(indexed_at) FROM albums WHERE indexed_at IS NOT NULL"
    ).fetchone()
    (last_disc,) = con.execute(
        "SELECT MAX(discovered_at) FROM albums"
    ).fetchone()
    # Pending must match the queue predicate: albums with attempts >= 5 are
    # never picked up again, so report them separately instead of as pending.
    (pending,) = con.execute(
        "SELECT COUNT(*) FROM albums"
        " WHERE dead = 0 AND attempts < 5 AND indexed_at IS NULL"
    ).fetchone()
    (stuck,) = con.execute(
        "SELECT COUNT(*) FROM albums WHERE dead = 0 AND attempts >= 5"
    ).fetchone()
    return {
        "albums_total": total,
        "albums_indexed": indexed,
        "albums_dead": dead,
        "albums_pending": pending,
        "albums_stuck": stuck,
        "files": files,
        "last_indexed_at": last_idx,
        "last_discovered_at": last_disc,
    }
