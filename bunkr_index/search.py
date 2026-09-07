"""Filename / album-title search over SQLite + FTS5 trigram indexes."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from typing import Any, Optional

# Runs of spaces/underscores/dashes: treated as equivalent separators when
# the user types one separator but the filename uses another.
_SEP = re.compile(r"[\s_\-+.]+", re.UNICODE)

# Total-count cap for pagination. Counting every match on a broad query gets
# expensive at millions of rows; above the cap we report `total = CAP` plus
# `truncated: true` (the pager still works, it just cannot show the exact
# last page). Cap+1 is fetched internally to disambiguate exact==cap.
COUNT_CAP = 50_000

MEDIA_GROUPS = {
    "image": ("Image", "GIF"),
    "video": ("Video",),
    "audio": ("Audio",),
    "other": ("",),
}


def _norm(q: str) -> str:
    return q.strip().lower()


def fts_variants(q: str) -> list[str]:
    """Phrase variants for trigram MATCH. Trigrams index raw substrings, so a
    quoted phrase finds the literal sequence anywhere in the name. Space is a
    token boundary for the trigram tokenizer, so typed spaces are also mapped
    to '_', '-' and removed entirely to hit separator-style filenames."""
    q = _norm(q)
    variants = [q]
    if len(q) >= 3 and _SEP.search(q):
        for repl in ("_", "-", ""):
            alt = _SEP.sub(repl, q)
            if alt != q and len(alt) >= 3:
                variants.append(alt)
    seen: list[str] = []
    for v in variants:
        if v not in seen:
            seen.append(v)
    return seen


def _fts_phrase(v: str) -> str:
    """Quote one phrase for FTS5 (trigram): inner quotes doubled."""
    return '"' + v.replace('"', '""') + '"'


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

_QUERY_PART = re.compile(r'"([^"]+)"|(\S+)', re.UNICODE)


def fts_query(q: str) -> str | None:
    """Build an FTS5 query.

    Unquoted words of three or more characters are required independently, so
    ``diora audition`` finds a filename containing intervening text. Quoted
    input remains an exact substring phrase. Queries containing a short word
    retain the old whole-phrase behavior because trigram cannot index that
    word independently.
    """
    q = q.strip()
    if len(_norm(q)) < 3:
        return None
    parts: list[str] = []
    for match in _QUERY_PART.finditer(q):
        quoted = match.group(1)
        if quoted is not None:
            parts.append(quoted)
            continue
        word = match.group(2)
        parts.extend(part for part in _SEP.split(word) if part)
    if not parts or any(len(_norm(part)) < 3 for part in parts):
        parts = [q]

    clauses = []
    for part in parts:
        variants = [v for v in fts_variants(part) if len(v) >= 3]
        if not variants:
            return None
        clauses.append(" OR ".join(_fts_phrase(v) for v in variants))
    if len(clauses) == 1:
        return clauses[0]
    return " AND ".join(f"({clause})" for clause in clauses)


def _cursor_context(q: str, media: Optional[str], extension: Optional[str],
                    sort: str, mode: str) -> dict[str, str]:
    return {
        "q": q, "media": media or "", "extension": extension or "",
        "sort": sort, "mode": mode,
    }


def _encode_cursor(context: dict[str, str], key: dict[str, Any]) -> str:
    payload = {**context, "key": key}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(token: Optional[str], context: dict[str, str]) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid search cursor") from None
    if not isinstance(payload, dict) or any(payload.get(k) != v for k, v in context.items()):
        raise ValueError("search cursor does not match this query")
    key = payload.get("key")
    if not isinstance(key, dict):
        raise ValueError("invalid search cursor")
    return key


def _media_where(media: Optional[str]) -> tuple[str, list]:
    if not media:
        return "", []
    group = MEDIA_GROUPS.get(media.lower())
    if not group:
        return "", []
    if group == ("",):
        # 'other': everything not image/video/audio
        known = tuple(
            v for g in ("image", "video", "audio") for v in MEDIA_GROUPS[g]
        )
        marks = ",".join("?" * len(known))
        return "AND f.media NOT IN (%s)" % marks, list(known)
    marks = ",".join("?" * len(group))
    return "AND f.media IN (%s)" % marks, list(group)

_EXTENSION = re.compile(r"^[a-z0-9]{1,16}$", re.ASCII)


def _extension_where(extension: Optional[str]) -> tuple[str, list]:
    if not extension:
        return "", []
    value = extension.removeprefix(".").casefold()
    if not _EXTENSION.fullmatch(value):
        raise ValueError("extension must contain 1-16 ASCII letters or digits")
    return "AND f.extension = ?", [value]


def _capped_total(con: sqlite3.Connection, inner_sql: str, params: list,
                  cap: int = COUNT_CAP) -> tuple[int, bool]:
    """COUNT over `inner_sql`, stopping after cap+1 rows. Returns (total,
    truncated)."""
    total = con.execute(
        f"SELECT COUNT(*) FROM ({inner_sql} LIMIT ?)", params + [cap + 1]
    ).fetchone()[0]
    if total > cap:
        return cap, True
    return total, False


_FILES_COLUMNS = """
f.id, f.album_id, f.file_id, f.search_name, f.storage, f.slug,
f.mime, f.media, f.extension, f.size, f.uploaded_at, a.title AS album_title
"""

_NEWEST_ORDER = "f.uploaded_at DESC NULLS LAST, f.id DESC"
_OLDEST_ORDER = "f.uploaded_at ASC NULLS LAST, f.id ASC"
_SIZE_ORDER = "f.size DESC, f.id DESC"
_NAME_ORDER = "f.search_name COLLATE NOCASE ASC, f.id ASC"


def _results(con: sqlite3.Connection, sql: str, params: list) -> list[dict]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def _order_spec(mode: str, sort: str) -> tuple[str, str]:
    if mode == "fts" and sort == "relevance":
        return "score ASC, rowid DESC", "relevance"
    if sort == "oldest":
        return _OLDEST_ORDER, "oldest"
    if sort == "size":
        return _SIZE_ORDER, "size"
    if mode == "prefix" and sort == "relevance":
        return _NAME_ORDER, "name"
    return _NEWEST_ORDER, "newest"


def _cursor_predicate(kind: str, key: dict[str, Any], *, score: str = "score",
                      rowid: str = "rowid") -> tuple[str, list]:
    """Return a keyset predicate for a decoded cursor."""
    try:
        file_id = int(key["id"])
        if kind == "relevance":
            value = float(key["score"])
            return (
                f"AND ({score} > ? OR ({score} = ? AND {rowid} < ?))",
                [value, value, file_id],
            )
        if kind == "name":
            value = str(key["search_name"])
            return (
                "AND (f.search_name COLLATE NOCASE > ? OR "
                "(f.search_name COLLATE NOCASE = ? AND f.id > ?))",
                [value, value, file_id],
            )
        if kind == "size":
            value = int(key["size"])
            return (
                "AND (f.size < ? OR (f.size = ? AND f.id < ?))",
                [value, value, file_id],
            )
        value = key.get("uploaded_at")
        if value is None:
            direction = "<" if kind == "newest" else ">"
            return f"AND (f.uploaded_at IS NULL AND f.id {direction} ?)", [file_id]
        direction = "<" if kind == "newest" else ">"
        id_direction = "<" if kind == "newest" else ">"
        return (
            "AND (f.uploaded_at IS NULL OR f.uploaded_at "
            f"{direction} ? OR (f.uploaded_at = ? AND f.id {id_direction} ?))",
            [value, value, file_id],
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid search cursor") from None


def _cursor_key(kind: str, row: dict) -> dict[str, Any]:
    key: dict[str, Any] = {"id": row["id"]}
    if kind == "relevance":
        key["score"] = row["_score"]
    elif kind == "name":
        key["search_name"] = row["search_name"]
    elif kind == "size":
        key["size"] = row["size"]
    else:
        key["uploaded_at"] = row["uploaded_at"]
    return key


def _finish_page(rows: list[dict], limit: int, context: dict[str, str],
                 kind: str) -> tuple[list[dict], bool, str | None]:
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        _encode_cursor(context, _cursor_key(kind, rows[-1]))
        if has_more and rows else None
    )
    for row in rows:
        row.pop("_score", None)
    return rows, has_more, next_cursor


def _file_rows_from_hits(con: sqlite3.Connection, hits_sql: str, params: list,
                         order: str) -> list[dict]:
    return _results(
        con,
        f"""
        WITH hits AS ({hits_sql})
        SELECT {_FILES_COLUMNS}
        FROM hits
        JOIN files f ON f.id = hits.rowid
        JOIN albums a ON a.bunkr_id = f.album_id
        ORDER BY {order}
        """,
        params,
    )


def search_files(
    con: sqlite3.Connection,
    q: str = "",
    media: Optional[str] = None,
    extension: Optional[str] = None,
    sort: str = "relevance",
    limit: int = 20,
    offset: int = 0,
    cursor: Optional[str] = None,
) -> dict:
    """Search file metadata with capped counts and cursor-capable pagination."""
    q = q.strip()
    limit = max(1, limit)
    media_where, media_params = _media_where(media)
    extension_where, extension_params = _extension_where(extension)
    filter_where = media_where + extension_where
    filter_params = media_params + extension_params
    match_clause = fts_query(q) if q else None
    mode = "list" if not q else "fts" if match_clause else "prefix"
    order, kind = _order_spec(mode, sort)
    context = _cursor_context(q, media, extension, sort, mode)
    cursor_key = _decode_cursor(cursor, context)
    page_offset = 0 if cursor_key is not None else max(0, offset)

    if mode == "list":
        total, truncated = _capped_total(
            con, "SELECT 1 FROM files f WHERE 1=1 " + filter_where, filter_params
        )
        cursor_where, cursor_params = (
            _cursor_predicate(kind, cursor_key) if cursor_key else ("", [])
        )
        hits = (
            "SELECT f.id AS rowid FROM files f WHERE 1=1 "
            f"{filter_where} {cursor_where} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = _file_rows_from_hits(
            con, hits, filter_params + cursor_params + [limit + 1, page_offset], order
        )
    elif mode == "prefix":
        like = _like_escape(q) + "%"
        where = "f.search_name COLLATE NOCASE LIKE ? ESCAPE '\\'" + filter_where
        total, truncated = _capped_total(
            con, "SELECT 1 FROM files f WHERE " + where, [like] + filter_params
        )
        cursor_where, cursor_params = (
            _cursor_predicate(kind, cursor_key) if cursor_key else ("", [])
        )
        hits = (
            "SELECT f.id AS rowid FROM files f WHERE "
            f"{where} {cursor_where} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = _file_rows_from_hits(
            con,
            hits,
            [like] + filter_params + cursor_params + [limit + 1, page_offset],
            order,
        )
    else:
        inner = "SELECT files_fts.rowid FROM files_fts WHERE files_fts MATCH ?"
        total_params: list[Any] = [match_clause]
        if filter_where:
            inner = (
                f"SELECT x.rowid FROM ({inner}) x "
                f"JOIN files f ON f.id = x.rowid WHERE 1=1 {filter_where}"
            )
            total_params += filter_params
        total, truncated = _capped_total(con, inner, total_params)

        if kind == "relevance":
            cursor_where, cursor_params = (
                _cursor_predicate(kind, cursor_key) if cursor_key else ("", [])
            )
            source = "FROM files_fts"
            if filter_where:
                source += " JOIN files f ON f.id = files_fts.rowid"
            rows = _results(
                con,
                f"""
                WITH scored AS (
                    SELECT files_fts.rowid AS rowid, bm25(files_fts) AS score
                    {source}
                    WHERE files_fts MATCH ? {filter_where}
                ),
                hits AS (
                    SELECT rowid, score FROM scored
                    WHERE 1=1 {cursor_where}
                    ORDER BY score ASC, rowid DESC
                    LIMIT ? OFFSET ?
                )
                SELECT {_FILES_COLUMNS}, hits.score AS _score
                FROM hits
                JOIN files f ON f.id = hits.rowid
                JOIN albums a ON a.bunkr_id = f.album_id
                ORDER BY hits.score ASC, f.id DESC
                """,
                [match_clause] + filter_params + cursor_params + [limit + 1, page_offset],
            )
        else:
            cursor_where, cursor_params = (
                _cursor_predicate(kind, cursor_key) if cursor_key else ("", [])
            )
            hits = (
                "SELECT f.id AS rowid FROM files_fts "
                "JOIN files f ON f.id = files_fts.rowid "
                f"WHERE files_fts MATCH ? {filter_where} {cursor_where} "
                f"ORDER BY {order} LIMIT ? OFFSET ?"
            )
            rows = _file_rows_from_hits(
                con,
                hits,
                [match_clause] + filter_params + cursor_params + [limit + 1, page_offset],
                order,
            )

    rows, has_more, next_cursor = _finish_page(rows, limit, context, kind)
    return {
        "query": q,
        "mode": mode,
        "total": total,
        "truncated": truncated,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "results": rows,
    }


def search_albums(
    con: sqlite3.Connection,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Album-title search (used for discovery/autocomplete of album names)."""
    q = q.strip()
    if not q:
        rows = con.execute(
            """
            SELECT bunkr_id, title, file_count, thumb, indexed_at IS NOT NULL AS indexed,
                   (SELECT COUNT(*) FROM files f WHERE f.album_id = a.bunkr_id) AS real_files
            FROM albums a
            ORDER BY COALESCE(updated_at, discovered_at) DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {"query": "", "total": 0, "results": [dict(r) for r in rows]}

    match_clause = fts_query(q)
    if match_clause:
        total = con.execute(
            "SELECT COUNT(*) FROM albums_fts WHERE albums_fts MATCH ?",
            (match_clause,),
        ).fetchone()[0]
        rows = con.execute(
            """
            SELECT a.bunkr_id, a.title, a.file_count, a.thumb,
                   a.indexed_at IS NOT NULL AS indexed,
                   (SELECT COUNT(*) FROM files f WHERE f.album_id = a.bunkr_id) AS real_files
            FROM albums a
            JOIN albums_fts ON albums_fts.rowid = a.id
            WHERE albums_fts MATCH ?
            ORDER BY bm25(albums_fts), a.id
            LIMIT ? OFFSET ?
            """,
            (match_clause, limit, offset),
        ).fetchall()
        return {"query": q, "total": total, "results": [dict(r) for r in rows]}

    # q < 3 chars: prefix match on title (album table is small).
    like = _like_escape(q) + "%"
    rows = con.execute(
        """
        SELECT bunkr_id, title, file_count, thumb,
               indexed_at IS NOT NULL AS indexed,
               (SELECT COUNT(*) FROM files f WHERE f.album_id = a.bunkr_id) AS real_files
        FROM albums a
        WHERE a.title COLLATE NOCASE LIKE ? ESCAPE '\\'
        ORDER BY a.title LIMIT ? OFFSET ?
        """,
        (like, limit, offset),
    ).fetchall()
    return {"query": q, "total": len(rows), "results": [dict(r) for r in rows]}
