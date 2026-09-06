"""Standalone discovery and metadata synchronization process.

Run this separately from ``serve``. It owns the crawler lock for the entire
``discover -> crawl`` cycle, writes the shared SQLite database, then repeats
after the configured interval. The UI only reads that database (plus its
explicit add/delete API operations) and never starts network scraping.
"""

from __future__ import annotations

import logging
import time

from . import config

log = logging.getLogger("sync")


def run_cycle(
    *,
    db_path=None,
    workers: int = config.DEFAULT_WORKERS,
    min_interval: float = config.MIN_REQUEST_INTERVAL,
    stale_days: int | None = None,
    discover_workers: int = 4,
    discover_delay: float = 0.08,
    full_discover: bool = False,
) -> dict:
    """Run one discovery and queue-drain cycle under the shared crawl lock."""
    from . import crawl, discover

    if not crawl.acquire_crawl_lock():
        log.warning("sync: another crawler is running; cycle skipped")
        return {"skipped": "lock"}
    try:
        path = db_path or config.DB_PATH
        log.info("sync cycle started: discover + crawl")
        discovered = discover.run(
            db_path=path,
            stop_when_known=not full_discover,
            workers=discover_workers,
            delay=discover_delay,
        )
        log.info("sync discover: %s", discovered)
        crawled = crawl.run(
            db_path=path,
            workers=workers,
            min_interval=min_interval,
            stale_days=stale_days,
        )
        log.info("sync crawl: %s", crawled)
        return {"discover": discovered, "crawl": crawled}
    finally:
        crawl.release_crawl_lock()


def run(
    *,
    db_path=None,
    workers: int = config.DEFAULT_WORKERS,
    min_interval: float = config.MIN_REQUEST_INTERVAL,
    stale_days: int | None = None,
    discover_workers: int = 4,
    discover_delay: float = 0.08,
    interval_hours: float = 24.0,
    once: bool = False,
    full_discover: bool = False,
) -> None:
    """Run sync once or continuously until interrupted."""
    interval_seconds = max(interval_hours, 0.0) * 3600
    while True:
        try:
            summary = run_cycle(
                db_path=db_path,
                workers=workers,
                min_interval=min_interval,
                stale_days=stale_days,
                discover_workers=discover_workers,
                discover_delay=discover_delay,
                full_discover=full_discover,
            )
            log.info("sync cycle finished: %s", summary)
        except Exception:
            log.exception("sync cycle failed; will retry")
            if once:
                raise
        if once:
            return
        log.info("sync: next cycle in %.1fh", interval_seconds / 3600)
        time.sleep(interval_seconds)
