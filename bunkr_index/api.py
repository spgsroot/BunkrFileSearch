"""Litestar application: file search API + static frontend."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Annotated, Any
from litestar import Litestar, delete, get, post
from litestar.exceptions import HTTPException
from litestar.openapi.config import OpenAPIConfig
from litestar.static_files import create_static_files_router
from . import config, db, search

_FRONTEND = Path(config.FRONTEND_DIR)
_ALBUM_ID_RE = re.compile(r"/a/([A-Za-z0-9]+)")


def _init_schema() -> None:
    """Prepare schema once when the UI process starts."""
    con = db.connect()
    try:
        db.init_db(con)
    finally:
        con.close()


def _con():
    return db.connect()


@get("/api/search", sync_to_thread=True)
def api_search(
    q: Annotated[str, QueryParameter(default="", max_length=200)],
    media: Annotated[
        str | None,
        QueryParameter(default=None, pattern=r"^(|image|video|audio|other)$"),
    ],
    sort: Annotated[
        str,
        QueryParameter(default="relevance", pattern=r"^(relevance|newest|oldest|size)$"),
    ],
    page: Annotated[int, QueryParameter(default=1, ge=1)],
    per: Annotated[int, QueryParameter(default=20, ge=1, le=100)],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    con = _con()
    try:
        out = search.search_files(
            con, q=q, media=(media or None), sort=sort,
            limit=per, offset=(page - 1) * per,
        )
    finally:
        con.close()
    out.update(page=page, per=per, took_ms=round((time.perf_counter() - t0) * 1000))
    return out


@get("/api/albums", sync_to_thread=True)
def api_albums(
    q: Annotated[str, QueryParameter(default="", max_length=200)],
    page: Annotated[int, QueryParameter(default=1, ge=1)],
    per: Annotated[int, QueryParameter(default=20, ge=1, le=100)],
    indexed: Annotated[bool | None, QueryParameter(default=None)],
) -> dict[str, Any]:
    con = _con()
    try:
        if q:
            out = search.search_albums(con, q=q, limit=per, offset=(page - 1) * per)
        else:
            params: list[Any] = []
            where = ""
            if indexed is not None:
                where = "WHERE a.indexed_at IS NOT NULL" if indexed else "WHERE a.indexed_at IS NULL"
            total = con.execute(
                "SELECT COUNT(*) FROM albums a " + where, params
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT a.bunkr_id, a.title, a.file_count, a.thumb,
                       a.indexed_at IS NOT NULL AS indexed,
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
        out.update(page=page, per=per)
        return out
    finally:
        con.close()


@post("/api/albums", sync_to_thread=True, status_code=200)
def api_add_albums(data: dict[str, Any]) -> dict[str, Any]:
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
        m = _ALBUM_ID_RE.search(u)
        if m:
            ids.append(m.group(1))
    if not ids:
        raise HTTPException(status_code=400, detail="no /a/<id> found in provided urls")
    con = _con()
    try:
        accepted = db.set_album_pending(con, ids)
    finally:
        con.close()
    return {
        "accepted": accepted,
        "note": "run `uv run bunkr-index sync` (or `crawl`) to index them",
    }


@delete("/api/albums/{bunkr_id:str}", sync_to_thread=True, status_code=200)
def api_delete_album(bunkr_id: str) -> dict[str, str]:
    con = _con()
    try:
        cur = con.execute("DELETE FROM albums WHERE bunkr_id = ?", (bunkr_id,))
        con.commit()
    finally:
        con.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="album not found")
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
    return (_FRONTEND / "index.html").read_text(encoding="utf-8")


app = Litestar(
    route_handlers=[
        api_search,
        api_albums,
        api_add_albums,
        api_delete_album,
        api_stats,
        index,
        create_static_files_router(
            path="/static", directories=[_FRONTEND], html_mode=True
        ),
    ],
    on_startup=[_init_schema],
    openapi_config=OpenAPIConfig(
        title="Bunkr File Index", version="0.1.0"
    ),
)
