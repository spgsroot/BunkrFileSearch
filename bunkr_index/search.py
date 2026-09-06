"""Filename / album-title search over SQLite + FTS5 trigram indexes."""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

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


_FILES_SELECT = """
SELECT f.id, f.album_id, f.file_id, f.search_name, f.storage, f.slug,
       f.mime, f.media, f.size, f.uploaded_at,
       a.title AS album_title
FROM files f
JOIN albums a ON a.bunkr_id = f.album_id
"""

# Composite index (uploaded_at DESC, id DESC) matches this exactly, so the
# planner can walk it in order with no temp sort and stop at LIMIT.
_NEWEST_ORDER = "f.uploaded_at DESC NULLS LAST, f.id DESC"


def _results(con: sqlite3.Connection, sql: str, params: list) -> list[dict]:
    rows = con.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def search_files(
    con: sqlite3.Connection,
    q: str = "",
    media: Optional[str] = None,
    sort: str = "relevance",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Filename search.

    - q >= 3 chars: FTS5 trigram substring search (with separator-tolerant
      variants), BM25 relevance by default.
    - q of 1-2 chars: prefix match via the search_name index (substring
      search below three chars is not indexable by trigram; a contains-scan
      over millions of rows is the cost we avoid).
    - empty q: newest-first listing.
    """
    q = q.strip()
    media_where, media_params = _media_where(media)

    if not q:
        total, truncated = _capped_total(
            con,
            "SELECT 1 FROM files f WHERE 1=1 " + media_where,
            media_params,
        )
        rows = _results(
            con,
            _FILES_SELECT
            + f"WHERE 1=1 {media_where} ORDER BY {_NEWEST_ORDER} LIMIT ? OFFSET ?",
            media_params + [limit, offset],
        )
        return {"query": q, "mode": "list", "total": total,
                "truncated": truncated, "results": rows}

    variants = [v for v in fts_variants(q) if len(v) >= 3]

    if variants:
        match_clause = " OR ".join(_fts_phrase(v) for v in variants)
        inner = "SELECT fts.rowid FROM files_fts fts WHERE files_fts MATCH ?"
        params = [match_clause]
        if media_where:
            inner = (
                f"SELECT x.rowid FROM ({inner}) x "
                f"JOIN files f ON f.id = x.rowid WHERE 1=1 {media_where}"
            )
            params = params + media_params
        total, truncated = _capped_total(con, inner, params)
        if sort == "newest":
            order = _NEWEST_ORDER
        elif sort == "oldest":
            order = "f.uploaded_at ASC NULLS LAST, f.id ASC"
        elif sort == "size":
            order = "f.size DESC, f.id DESC"
        else:
            order = "bm25(files_fts), f.id DESC"
        rows = _results(
            con,
            _FILES_SELECT
            + "JOIN files_fts ON files_fts.rowid = f.id "
            + f"WHERE files_fts MATCH ? {media_where} "
            + f"ORDER BY {order} LIMIT ? OFFSET ?",
            [match_clause] + media_params + [limit, offset],
        )
        return {"query": q, "mode": "fts", "total": total,
                "truncated": truncated, "results": rows}

    # q < 3 chars: prefix match, index-range driven (idx_files_search).
    # Ordered by name (not upload date): keeping the newest-order here would
    # force a full-index scan, which is the cost this branch exists to avoid.
    like = _like_escape(q) + "%"
    where = "f.search_name LIKE ? ESCAPE '\\'" + media_where
    total, truncated = _capped_total(
        con,
        "SELECT 1 FROM files f WHERE " + where,
        [like] + media_params,
    )
    rows = _results(
        con,
        _FILES_SELECT
        + f"WHERE {where} ORDER BY f.search_name ASC LIMIT ? OFFSET ?",
        [like] + media_params + [limit, offset],
    )
    return {"query": q, "mode": "prefix", "total": total,
            "truncated": truncated, "results": rows}


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

    variants = [v for v in fts_variants(q) if len(v) >= 3]
    if variants:
        match_clause = " OR ".join(_fts_phrase(v) for v in variants)
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
        WHERE a.title LIKE ? ESCAPE '\\'
        ORDER BY a.title LIMIT ? OFFSET ?
        """,
        (like, limit, offset),
    ).fetchall()
    return {"query": q, "total": len(rows), "results": [dict(r) for r in rows]}
