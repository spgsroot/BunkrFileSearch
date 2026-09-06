"""balbums.st discovery crawler.

Walks the public album directory (sorted newest-first, up to ~398k albums
across ~3984 pages at per=100) and upserts {id, title, file_count} records
into SQLite. Checkpointed per page; resume with --start-page.

The default run covers every page; the run is idempotent, so a plain
`discover` after the first pass simply refreshes titles/counts. Pages are
fetched with bounded parallelism but committed in page order, so stop
conditions (--stop-known / empty tail) stay deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

import httpx

from . import config, db, parse

log = logging.getLogger("discover")


def _page_url(page: int, per: int, sort: str) -> str:
    return (
        f"{config.DISCOVER_BASE}?search=&mode=broad&per={per}"
        f"&sort={sort}&page={page}"
    )


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url)
    r.raise_for_status()
    return r.text


async def discover(
    db_path,
    start_page: int = 1,
    max_pages: int | None = None,
    per: int = 100,
    sort: str = "latest",
    delay: float = 0.08,
    stop_when_known: bool = False,
    workers: int = 4,
) -> dict:
    """Crawl discovery pages with `workers` concurrent fetches, committing
    results in page order. Returns summary dict."""
    summary = {"pages": 0, "cards": 0, "new_albums": 0, "errors": 0, "stopped": ""}
    t_start = time.monotonic()
    async with httpx.AsyncClient(
        headers=config.HEADERS, timeout=config.TIMEOUT, follow_redirects=True
    ) as client:
        pending: deque[tuple[int, asyncio.Task]] = deque()
        next_page = start_page

        def slot_free() -> bool:
            if max_pages is not None and next_page - start_page >= max_pages:
                return False
            return len(pending) < workers

        while True:
            # Prefetch up to `workers` pages ahead.
            while slot_free():
                page = next_page
                next_page += 1
                pending.append(
                    (page, asyncio.create_task(_fetch_page(client, _page_url(page, per, sort))))
                )

            if not pending:
                summary["stopped"] = (
                    "max_pages" if max_pages is not None else "pages_exhausted"
                )
                break

            # Commit strictly in page order.
            page, task = pending.popleft()
            try:
                text = await task
            except httpx.HTTPError as exc:
                log.warning("page %d failed: %s", page, exc)
                summary["errors"] += 1
                if summary["errors"] >= 5:
                    summary["stopped"] = "repeated_errors"
                    for _, t in pending:
                        t.cancel()
                    break
                continue

            cards = parse.parse_balbums_page(text)
            summary["pages"] += 1
            summary["cards"] += len(cards)
            if not cards:
                summary["stopped"] = "empty_page"
                for _, t in pending:
                    t.cancel()
                break

            con = db.connect(db_path)
            try:
                new_count = db.upsert_discovered(con, cards)
            finally:
                con.close()
            summary["new_albums"] += new_count

            if stop_when_known and new_count == 0:
                # Reached the region already fully discovered on this run's
                # ordering; the tail of the directory is already known.
                summary["stopped"] = "all_known"
                for _, t in pending:
                    t.cancel()
                break

            if summary["pages"] % 25 == 0:
                elapsed = time.monotonic() - t_start
                log.info(
                    "page %d: %d cards (%d new) total_new=%d  %.1f pages/s",
                    page, len(cards), new_count, summary["new_albums"],
                    summary["pages"] / elapsed if elapsed else 0.0,
                )
            if delay:
                await asyncio.sleep(delay)

    if not summary["stopped"] and summary["pages"]:
        summary["stopped"] = "max_pages" if max_pages is not None else "complete"
    return summary


def run(db_path=None, **kwargs) -> dict:
    """Crawl the directory. By default walks every page (idempotent upserts);
    pass stop_when_known=True to end early once only already-known albums
    appear (useful for frequent incremental refreshes)."""
    db_path = db_path or config.DB_PATH
    con = db.connect(db_path)
    try:
        db.init_db(con)
    finally:
        con.close()
    return asyncio.run(discover(db_path, **kwargs))
