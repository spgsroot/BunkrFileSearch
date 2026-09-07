"""Command-line entry: `python -m bunkr_index <command>`.

Commands:
  init                  create/verify the SQLite schema
  discover              crawl balbums.st album directory into the DB
  crawl                 fetch album metadata for pending/queued albums
  sync                 run discovery and metadata crawling in a separate process
  serve                 run the Litestar search server + UI only
  add <url|id> ...      enqueue specific albums for crawling
  albums [--q X]        list/search known albums
  stats                 database counts
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config, db

LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _init(args):
    con = db.connect()
    try:
        db.init_db(con)
    finally:
        con.close()
    print(f"schema ready at {config.DB_PATH}")


def _discover(args):
    from . import discover

    s = discover.run(
        start_page=args.start_page,
        max_pages=args.pages,
        per=args.per,
        sort=args.sort,
        delay=args.delay,
        workers=args.workers,
        stop_when_known=args.stop_known,
    )
    print(f"discover done: {s}")
    if s["stopped"] == "all_known":
        print("stopped early (--stop-known): only already-known albums on this page")
    if s["new_albums"]:
        print(
            f"{s['new_albums']} new albums found - "
            "run `uv run bunkr-index sync` (or `crawl`) to index them"
        )


def _crawl(args):
    from . import crawl

    if not crawl.acquire_crawl_lock():
        print("another crawler is running (sync or manual crawl) — skipping this run")
        return
    try:
        ids = None
        if args.ids:
            ids = args.ids
        s = crawl.run(
            ids=ids,
            limit=args.limit,
            workers=args.workers,
            stale_days=args.stale_days,
            min_interval=args.min_interval,
        )
    finally:
        crawl.release_crawl_lock()
    print(f"crawl done: {s}")
    if not ids and s.get("stopped") == "queue_empty":
        print("nothing pending - run `uv run bunkr-index discover` first to find new albums,")
        print("or pass --ids <album-id> to index a specific album")

def _sync(args):
    from . import sync

    sync.run(
        workers=args.workers,
        min_interval=args.min_interval,
        stale_days=args.stale_days,
        discover_workers=args.discover_workers,
        discover_delay=args.discover_delay,
        interval_hours=args.interval_hours,
        once=args.once,
        full_discover=args.full_discover,
    )

def _serve(args):
    import uvicorn

    uvicorn.run("bunkr_index.api:app", host=args.host, port=args.port, reload=False)


def _add(args):
    from . import parse

    ids = []
    for raw in args.ids:
        album_id = parse.extract_album_id(raw)
        if album_id is None:
            print(f"skipping {raw!r}: not an album URL or id", file=sys.stderr)
            continue
        ids.append(album_id)
    if not ids:
        print("nothing to enqueue: no valid album URLs or ids given", file=sys.stderr)
        return
    con = db.connect()
    try:
        db.init_db(con)
        accepted = db.set_album_pending(con, ids)
    finally:
        con.close()
    print(f"enqueued {len(accepted)} albums; run `uv run bunkr-index sync` (or `crawl`) to index")


def _albums(args):
    from . import search

    con = db.connect()
    try:
        out = search.search_albums(con, q=args.q, limit=args.limit, offset=0)
    finally:
        con.close()
    print(f"total (approx) {out['total']}")
    for r in out["results"][: args.limit]:
        idx = "indexed" if r["indexed"] else "pending"
        print(f"{r['bunkr_id']:12s} [{idx:7s}] {r['real_files']:>6d} files  {r['title']}")


def _stats(args):
    con = db.connect()
    try:
        s = db.stats(con)
    finally:
        con.close()
    for k, v in s.items():
        print(f"{k:20s} {v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bunkr_index", description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create schema")
    p.set_defaults(fn=_init)

    p = sub.add_parser("discover", help="crawl balbums.st directory")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--pages", type=int, default=None, help="max pages (default: full directory)")
    p.add_argument("--per", type=int, choices=(20, 100), default=config.BALBUMS_PER)
    p.add_argument("--sort", default="latest", help="balbums sort order")
    p.add_argument("--workers", type=int, default=4,
                   help="concurrent page fetches (pages still committed in order)")
    p.add_argument("--delay", type=float, default=0.08,
                   help="min seconds between page dispatches")
    p.add_argument("--stop-known", action="store_true",
                   help="stop early when a page contains only already-known albums "
                        "(fast incremental refresh)")
    p.set_defaults(fn=_discover)

    p = sub.add_parser("crawl", help="fetch album metadata")
    p.add_argument("--limit", type=int, default=None, help="max albums this run")
    p.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS,
                   help="concurrent fetches")
    p.add_argument("--min-interval", type=float, default=config.MIN_REQUEST_INTERVAL,
                   help="min seconds between request starts (politeness; "
                        "throughput cap = 1/min-interval req/s)")
    p.add_argument("--stale-days", type=int, default=None,
                   help="also refresh albums indexed more than N days ago")
    p.add_argument("--ids", nargs="*", help="specific album ids/urls to crawl")
    p.set_defaults(fn=_crawl)

    p = sub.add_parser(
        "sync",
        help="discover albums and crawl metadata in a separate long-running process",
    )
    p.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS,
                   help="concurrent album metadata fetches")
    p.add_argument("--min-interval", type=float, default=config.MIN_REQUEST_INTERVAL,
                   help="min seconds between album request starts")
    p.add_argument("--stale-days", type=int, default=None,
                   help="also refresh albums indexed more than N days ago")
    p.add_argument("--discover-workers", type=int, default=4,
                   help="concurrent balbums.st page fetches")
    p.add_argument("--discover-delay", type=float, default=0.08,
                   help="min seconds between discovery page dispatches")
    p.add_argument("--interval-hours", type=float, default=24.0,
                   help="hours between sync cycle starts")
    p.add_argument("--once", action="store_true",
                   help="run one discover+crawl cycle and exit")
    p.add_argument("--full-discover", action="store_true",
                   help="scan every discovery page instead of stopping at known albums")
    p.set_defaults(fn=_sync)

    p = sub.add_parser("serve", help="run search server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=_serve)

    p = sub.add_parser("add", help="enqueue album urls/ids")
    p.add_argument("ids", nargs="+", help="album urls or bare ids")
    p.set_defaults(fn=_add)

    p = sub.add_parser("albums", help="list/search albums")
    p.add_argument("--q", default="")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=_albums)

    p = sub.add_parser("stats", help="database stats")
    p.set_defaults(fn=_stats)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format=LOG_FMT
    )
    # Per-request httpx INFO lines (every redirect hop etc.) are noise for
    # crawlers; keep them only with --verbose.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if args.verbose else logging.WARNING
    )
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
