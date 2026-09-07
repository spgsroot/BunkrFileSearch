"""Litestar application: file search API + static frontend."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

from litestar import Litestar, Request, delete, get, post
from litestar.exceptions import HTTPException
from litestar.openapi.config import OpenAPIConfig
from litestar.params import QueryParameter
from litestar.response import Response
from litestar.static_files import create_static_files_router

from . import cache as cache_module
from . import config, db, search
from .parse import ALBUM_ID_RE

_FRONTEND = Path(config.FRONTEND_DIR)
_DIST = _FRONTEND / "dist"


def _init_schema() -> None:
    """Prepare schema once when the UI process starts."""
    con = db.connect()
    try:
        db.init_db(con)
    finally:
        con.close()


def _con():
    return db.connect()


# Search responses are cached in-process for a short window: repeated or
# revalidated requests skip SQLite entirely, while the TTL keeps newly crawled
# metadata visible within about a minute.
_SEARCH_CACHE = cache_module.TTLCache(maxsize=1024, ttl=60.0)


def _cache_key(*parts: object) -> str:
    """Canonical cache key from a request's distinguishing parameters."""
    return "\x1f".join("" if part is None else str(part) for part in parts)


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha1(body).hexdigest() + '"'


def _etag_matches(header: str, etag: str) -> bool:
    """True if a (weak, possibly comma-separated) If-None-Match header matches."""
    expected = etag.strip()
    if expected.startswith("W/"):
        expected = expected[2:].strip()
    for token in header.split(","):
        token = token.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == expected:
            return True
    return False


async def _respond(
    request: Request,
    key: str,
    build: Callable[[], dict],
) -> Response:
    """Serve ``build()``'s payload through the TTL cache with ETag support.

    ``build`` runs off the event loop via ``asyncio.to_thread``. It must return
    JSON-serializable data without timing fields. ``took_ms`` measures how long
    ``build()`` actually took (0 on cache hits) and is appended after caching,
    so the cached body and ETag stay stable across requests.
    """
    cached = _SEARCH_CACHE.get(key)
    if cached is not None:
        body: bytes = cached
        took_ms = 0
        hit = True
    else:
        t0 = time.perf_counter()
        body = _json_body(await asyncio.to_thread(build))
        took_ms = round((time.perf_counter() - t0) * 1000)
        _SEARCH_CACHE.set(key, body)
        hit = False
    etag = _etag(body)
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache",
        "X-Cache": "hit" if hit else "miss",
    }
    if _etag_matches(request.headers.get("if-none-match") or "", etag):
        return Response(content=b"", status_code=304, headers=headers)
    payload = json.loads(body)
    payload["took_ms"] = took_ms
    body = _json_body(payload)
    return Response(content=body, media_type="application/json", headers=headers)


def _require_admin(request: Request) -> None:
    """Guard mutating endpoints when BUNKR_ADMIN_TOKEN is configured.

    With no token configured the API stays open (local self-hosted default);
    with a token set, enqueue/delete require `Authorization: Bearer <token>`
    or an `X-Admin-Token` header."""
    token = config.ADMIN_TOKEN
    if not token:
        return
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if hmac.compare_digest(bearer, token):
        return
    if hmac.compare_digest(request.headers.get("x-admin-token", ""), token):
        return
    raise HTTPException(
        status_code=401,
        detail="admin token required for mutating endpoints (BUNKR_ADMIN_TOKEN)",
    )


@get("/api/search")
async def api_search(
    request: Request,
    q: Annotated[str, QueryParameter(default="", max_length=200)],
    media: Annotated[
        str | None,
        QueryParameter(default=None, pattern=r"^(|image|video|audio|other)$"),
    ],
    ext: Annotated[
        str | None,
        QueryParameter(default=None, pattern=r"^[A-Za-z0-9]{1,16}$"),
    ],
    sort: Annotated[
        str,
        QueryParameter(default="relevance", pattern=r"^(relevance|newest|oldest|size)$"),
    ],
    page: Annotated[int, QueryParameter(default=1, ge=1)],
    per: Annotated[int, QueryParameter(default=20, ge=1, le=100)],
    cursor: Annotated[str | None, QueryParameter(default=None, max_length=1024)],
) -> Response:
    key = _cache_key(
        "/api/search", q, media or "", ext or "", sort, page, per, cursor or ""
    )

    def build() -> dict:
        con = _con()
        try:
            try:
                out = search.search_files(
                    con, q=q, media=(media or None), extension=(ext or None),
                    sort=sort, limit=per, offset=(page - 1) * per, cursor=cursor,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            con.close()
        out.update(page=page, per=per)
        return out

    return await _respond(request, key, build)


@get("/api/albums")
async def api_albums(
    request: Request,
    q: Annotated[str, QueryParameter(default="", max_length=200)],
    page: Annotated[int, QueryParameter(default=1, ge=1)],
    per: Annotated[int, QueryParameter(default=20, ge=1, le=100)],
    indexed: Annotated[bool | None, QueryParameter(default=None)],
) -> Response:
    key = _cache_key("/api/albums", q, page, per, "" if indexed is None else indexed)

    def build() -> dict:
        con = _con()
        try:
            if q:
                out = search.search_albums(
                    con, q=q, limit=per, offset=(page - 1) * per
                )
            else:
                params: list[Any] = []
                # Dead albums never belong in browse listings, whatever flag
                # their indexed_at happens to carry from an earlier crawl.
                where = "WHERE a.dead = 0"
                if indexed is not None:
                    where += (
                        " AND a.indexed_at IS NOT NULL"
                        if indexed
                        else " AND a.indexed_at IS NULL"
                    )
                total = con.execute(
                    "SELECT COUNT(*) FROM albums a " + where, params
                ).fetchone()[0]
                rows = con.execute(
                    """
                    SELECT a.bunkr_id, a.title, a.file_count, a.thumb,
                           a.indexed_at IS NOT NULL AS indexed, a.dead AS dead,
                           (SELECT COUNT(*) FROM files f WHERE f.album_id = a.bunkr_id)
                               AS real_files
                    FROM albums a
                    """ + where + """
                    ORDER BY COALESCE(a.updated_at, a.discovered_at) DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [per, (page - 1) * per],
                ).fetchall()
                out = {
                    "query": "",
                    "total": total,
                    "results": [dict(r) for r in rows],
                }
        finally:
            con.close()
        out.update(page=page, per=per)
        return out

    return await _respond(request, key, build)


_RANDOM_TRIES = 50
_RANDOM_SQL = """
SELECT a.bunkr_id, a.title, a.thumb, a.file_count,
       (SELECT COUNT(*) FROM files f WHERE f.album_id = a.bunkr_id) AS real_files,
       a.updated_at
FROM albums a
WHERE a.id >= ? AND a.indexed_at IS NOT NULL AND a.dead = 0
ORDER BY a.id LIMIT 1
"""


def _pick_random_album(con) -> dict | None:
    """Pick a random indexed album that still has files, via id sampling."""
    (max_id,) = con.execute("SELECT MAX(id) FROM albums").fetchone()
    if not max_id:
        return None
    for _ in range(_RANDOM_TRIES):
        row = con.execute(_RANDOM_SQL, (random.randint(1, max_id),)).fetchone()
        if row and row["real_files"]:
            return dict(row)
    return None


def _random_album_thread() -> dict | None:
    con = _con()
    try:
        return _pick_random_album(con)
    finally:
        con.close()


@get("/api/albums/random")
async def api_random_album() -> dict[str, Any]:
    """Return one random indexed album with files (uncached by design)."""
    album = await asyncio.to_thread(_random_album_thread)
    if album is None:
        raise HTTPException(status_code=404, detail="no indexed albums yet")
    return album


@post("/api/albums", sync_to_thread=True, status_code=200)
def api_add_albums(request: Request, data: dict[str, Any]) -> dict[str, Any]:
    _require_admin(request)
    urls = data.get("urls")
    if not isinstance(urls, list) or not urls:
        raise HTTPException(
            status_code=400,
            detail="payload: {'urls': ['https://<bunkr>/a/<id>', ...]}",
        )
    ids: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            continue
        m = ALBUM_ID_RE.search(u)
        if m:
            ids.append(m.group(1))
    if not ids:
        raise HTTPException(status_code=400, detail="no /a/<id> found in provided urls")
    con = _con()
    try:
        accepted = db.set_album_pending(con, ids)
    finally:
        con.close()
    _SEARCH_CACHE.clear()
    return {
        "accepted": accepted,
        "note": "run `uv run bunkr-index sync` (or `crawl`) to index them",
    }


@delete("/api/albums/{bunkr_id:str}", sync_to_thread=True, status_code=200)
def api_delete_album(request: Request, bunkr_id: str) -> dict[str, str]:
    _require_admin(request)
    con = _con()
    try:
        cur = con.execute("DELETE FROM albums WHERE bunkr_id = ?", (bunkr_id,))
        deleted = cur.rowcount
        con.commit()
    finally:
        con.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="album not found")
    _SEARCH_CACHE.clear()
    return {"deleted": bunkr_id}


@get("/api/stats", sync_to_thread=True)
def api_stats() -> dict[str, Any]:
    con = _con()
    try:
        result = db.stats(con)
    finally:
        con.close()
    db_path = Path(config.DB_PATH)
    result["db_bytes"] = db_path.stat().st_size if db_path.exists() else 0
    return result


@get("/", sync_to_thread=True, media_type="text/html")
def index() -> str:
    return (_DIST / "index.html").read_text(encoding="utf-8")


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # The frontend is a Svelte SPA: JS/CSS are external built assets, served
    # same-origin. Inline is not required.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' https: data:; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}


def _security_headers(response: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


app = Litestar(
    route_handlers=[
        api_search,
        api_albums,
        api_random_album,
        api_add_albums,
        api_delete_album,
        api_stats,
        index,
        create_static_files_router(
            path="/assets", directories=[_DIST / "assets"]
        ),
    ],
    on_startup=[_init_schema],
    after_request=_security_headers,
    openapi_config=OpenAPIConfig(
        title="Bunkr File Index", version="0.1.0"
    ),
)
