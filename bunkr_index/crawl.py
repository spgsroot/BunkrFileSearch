"""Bunkr album metadata crawler (workers, polite, resumable).

One GET per album (`/<domain>/a/<id>?advanced=1`) returns the full file list
inline; only metadata is stored, media is never downloaded. Domain rotation
kicks in on 403/429/5xx. Each album commits atomically, so interruption is
safe: rerun to continue from where it stopped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import time
from pathlib import Path

import httpx

from . import config, db, parse

log = logging.getLogger("crawl")

LOCK_FILE = config.DATA_DIR / "crawl.lock"


def _pid_alive(pid: int) -> bool:
    """Cross-platform process-alive probe (best effort)."""
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, 0, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True  # assume alive rather than clobbering a live run
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_start_ticks(pid: int) -> str | None:
    """Return the Linux /proc start time, which distinguishes reused PIDs."""
    if os.name == "nt":
        return None
    try:
        # After the final ')' are fields 3 onward; starttime is field 22.
        return (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1]
                .split()[19])
    except (OSError, IndexError):
        return None


def _lock_identity() -> str:
    """Identify this process without confusing a recreated Docker PID 1."""
    pid = os.getpid()
    start = _process_start_ticks(pid)
    return f"{pid}:{start}" if start is not None else str(pid)


def _lock_owner_is_alive(value: str) -> bool:
    """Return whether a lock owner still refers to the same process."""
    pid_text, separator, recorded_start = value.strip().partition(":")
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if not _pid_alive(pid):
        return False
    if not separator:
        # Legacy PID-only locks cannot distinguish a newly recreated Linux
        # container's PID 1 from its predecessor.
        return os.name == "nt" or pid != os.getpid()
    return _process_start_ticks(pid) == recorded_start


def _try_create_lock() -> bool:
    """Atomically create the lock file; False if it already exists."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(_lock_identity())
    return True


def acquire_crawl_lock() -> bool:
    """Acquire the exclusive, stale-safe lock for crawling.

    Creation is O_EXCL-atomic: two processes racing a missing or stale lock
    can no longer both win (check-then-write TOCTOU)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _try_create_lock():
        return True
    try:
        if _lock_owner_is_alive(LOCK_FILE.read_text()):
            return False
    except OSError:
        return False
    # Stale lock: remove it and retry the atomic create once. Whoever wins
    # the retry owns the crawl; the loser gets FileExistsError -> False.
    try:
        LOCK_FILE.unlink()
    except OSError:
        return False
    return _try_create_lock()


def release_crawl_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == _lock_identity():
            LOCK_FILE.unlink()
    except OSError:
        pass

# HTTP statuses that trigger trying another domain / retry.
_RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class DomainPicker:
    """Sticky domain selection with per-domain failure backoff.

    Traffic stays on one preferred domain (no pointless redirect churn through
    legacy domains like bunkr.ps, which 301 elsewhere); the picker only moves
    to another domain after a failure, and the first domain that then succeeds
    becomes the new preferred one."""

    def __init__(self, domains: list[str] | None = None) -> None:
        self.domains = list(domains or config.DOMAINS)
        self._preferred = self.domains[0]
        self._fail_until: dict[str, float] = {}

    def _healthy(self, dom: str) -> bool:
        return self._fail_until.get(dom, 0.0) <= time.monotonic()

    def next(self) -> str:
        if self._healthy(self._preferred):
            return self._preferred
        for dom in self.domains:
            if self._healthy(dom):
                return dom
        # Everything is cooling down: fall back to the preferred one anyway
        # (the caller will mark it failed again and we keep rotating).
        return self._preferred

    def mark_failure(self, dom: str, seconds: float = 30.0) -> None:
        self._fail_until[dom] = time.monotonic() + seconds

    def mark_success(self, dom: str) -> None:
        # Stick to whichever domain actually served the album.
        self._preferred = dom


class Breaker:
    """Global rate-limit circuit breaker shared by all workers.

    When consecutive album fetches all end in rate-limit-class statuses
    (403/429/5xx), the whole crawl pauses for a cooldown that doubles on each
    trip (10s -> 20s -> ... capped at 300s) and resets after any success."""

    def __init__(self, base: float = 10.0, cap: float = 300.0,
                 strikes: int = 2) -> None:
        self.base = base
        self.cap = cap
        self.strikes_needed = strikes
        self._strikes = 0
        self._backoff = base
        self._cooldown_until = 0.0

    def ok(self) -> None:
        self._strikes = 0
        self._backoff = self.base
        self._cooldown_until = 0.0

    def failure(self, now: float | None = None) -> float:
        """Register a rate-limited album. Returns cooldown seconds applied
        (0 if the breaker did not trip yet)."""
        now = time.monotonic() if now is None else now
        self._strikes += 1
        if self._strikes < self.strikes_needed:
            return 0.0
        self._strikes = 0
        seconds = self._backoff
        self._cooldown_until = now + seconds
        self._backoff = min(self._backoff * 2, self.cap)
        return seconds

    def wait_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._cooldown_until - now)


def _retry_after_seconds(r: httpx.Response) -> float | None:
    """Honor Retry-After on 429/503 when present (seconds)."""
    if r.status_code not in (429, 503):
        return None
    hdr = r.headers.get("Retry-After")
    if not hdr:
        return None
    try:
        return float(hdr.strip())
    except ValueError:
        return None


async def fetch_album(client: httpx.AsyncClient, picker: DomainPicker, album_id: str):
    """Return (status, parsed, rate_limited) for one album.

    status in {'ok','dead','error'}; rate_limited=True when every attempted
    domain answered with a rate-limit-class status (403/429/5xx) — the signal
    for the global circuit breaker."""
    last_exc = None
    rate_hits = 0
    attempts = 0
    tried: set[str] = set()
    while len(tried) < min(3, len(picker.domains)):
        dom = picker.next()
        if dom in tried:
            break
        tried.add(dom)
        attempts += 1
        url = f"https://{dom}/a/{album_id}?advanced=1"
        try:
            r = await client.get(url)
        except httpx.HTTPError as exc:
            last_exc = exc
            picker.mark_failure(dom)
            await asyncio.sleep(0.4 + random.random() * 0.6)
            continue
        if r.status_code == 404:
            return "dead", None, False
        if r.status_code in _RETRY_STATUS:
            picker.mark_failure(dom, seconds=_retry_after_seconds(r) or 30.0)
            rate_hits += 1
            last_exc = RuntimeError(f"HTTP {r.status_code}")
            await asyncio.sleep(0.3 + random.random() * 0.5)
            continue
        if r.status_code != 200:
            last_exc = RuntimeError(f"HTTP {r.status_code}")
            continue
        parsed = parse.parse_album_page(r.text)
        if parsed.get("parse_error") or not parsed.get("blob"):
            # 200 without the file blob: redirect/maintenance/challenge page.
            # Treat as retryable so the album is never stored as empty/indexed.
            picker.mark_failure(dom, seconds=5.0)
            last_exc = RuntimeError("album page returned no file data")
            continue
        picker.mark_success(dom)
        return "ok", parsed, False
    rate_limited = attempts > 0 and rate_hits == attempts
    return "error", last_exc and f"{type(last_exc).__name__}: {last_exc}", rate_limited


async def crawl_ids(
    db_path,
    bunkr_ids: list[str],
    workers: int = config.DEFAULT_WORKERS,
    min_interval: float = config.MIN_REQUEST_INTERVAL,
    breaker: Breaker | None = None,
) -> dict:
    summary = {"ok": 0, "dead": 0, "error": 0, "files": 0, "errors": []}
    if breaker is None:
        breaker = Breaker()
    con = db.connect(db_path)
    try:
        sem = asyncio.Semaphore(workers)
        last_request = time.monotonic()
        lock = asyncio.Lock()

        async def pace() -> None:
            nonlocal last_request
            async with lock:
                now = time.monotonic()
                wait = max(
                    min_interval - (now - last_request),
                    breaker.wait_seconds(now),
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                last_request = time.monotonic()

        async with httpx.AsyncClient(
            headers=config.HEADERS,
            timeout=config.TIMEOUT,
            follow_redirects=True,
        ) as client:
            picker = DomainPicker()
            completed = 0

            async def work(album_id: str) -> None:
                nonlocal completed
                async with sem:
                    await pace()
                    status, parsed, rate_limited = await fetch_album(
                        client, picker, album_id
                    )
                    if status == "ok":
                        breaker.ok()
                        try:
                            n = db.replace_album_files(
                                con, album_id, parsed["title"], parsed["thumb"],
                                parsed["files"],
                            )
                        except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
                            # One malformed album (duplicate file ids, bad row
                            # shape) must not abort the whole wave: record it
                            # as a normal per-album error and continue.
                            log.warning("db write failed for %s: %s", album_id, exc)
                            db.mark_album_error(con, album_id, f"db write: {exc}")
                            summary["error"] += 1
                            summary["errors"].append((album_id, f"db write: {exc}"))
                        else:
                            summary["ok"] += 1
                            summary["files"] += n
                            log.debug("ok %s: %d files", album_id, n)
                    elif status == "dead":
                        breaker.ok()
                        db.mark_album_error(con, album_id, "HTTP 404", dead=True)
                        summary["dead"] += 1
                    else:
                        db.mark_album_error(con, album_id, str(parsed))
                        summary["error"] += 1
                        summary["errors"].append((album_id, str(parsed)))
                        log.debug("error %s: %s", album_id, parsed)
                        if rate_limited:
                            pause = breaker.failure()
                            if pause:
                                log.warning(
                                    "rate-limited (403/429/5xx across domains): "
                                    "pausing crawl %.0fs", pause
                                )
                    completed += 1
                    if completed % 50 == 0 or completed == len(bunkr_ids):
                        log.info(
                            "progress %d/%d ok=%d err=%d dead=%d files=%d",
                            completed, len(bunkr_ids),
                            summary["ok"], summary["error"], summary["dead"],
                            summary["files"],
                        )

            # The semaphore already bounds concurrency; chunking into waves of
            # `workers` would make every wave wait for its slowest album.
            await asyncio.gather(*(work(b) for b in bunkr_ids))
    finally:
        con.close()
    return summary


async def crawl_all(
    db_path,
    limit: int | None = None,
    workers: int = config.DEFAULT_WORKERS,
    stale_days: int | None = None,
    min_interval: float = config.MIN_REQUEST_INTERVAL,
) -> dict:
    """Drain the pending queue (never-indexed albums, newest first) and, when
    `stale_days` is set, refresh albums not re-crawled within that window."""
    total = {"ok": 0, "dead": 0, "error": 0, "files": 0, "stopped": ""}
    con = db.connect(db_path)
    breaker = Breaker()
    processed = 0
    t_start = time.monotonic()
    try:
        while True:
            if limit is not None and processed >= limit:
                total["stopped"] = "limit"
                break
            rows = db.pending_for_crawl(
                con, limit=200, include_stale_days=stale_days
            )
            if not rows:
                total["stopped"] = "queue_empty"
                break
            ids = [r["bunkr_id"] for r in rows]
            take = len(ids)
            if limit is not None:
                take = min(take, limit - processed)
                ids = ids[:take]
            if not ids:
                break
            batch = await crawl_ids(
                db_path, ids, workers=workers, min_interval=min_interval,
                breaker=breaker,
            )
            for k in ("ok", "dead", "error", "files"):
                total[k] += batch[k]
            processed += take
            elapsed = time.monotonic() - t_start
            rate = processed / elapsed if elapsed else 0.0
            if rate:
                if limit is not None:
                    remaining = max(limit - processed, 0)
                else:
                    # Same WHERE as pending_for_crawl, so the ETA matches the
                    # queue being drained (single source lives in db.py).
                    remaining = db.count_pending_for_crawl(con, stale_days)
                eta_h = remaining / rate / 3600
            else:
                eta_h = 0.0
            log.info(
                "wave done: processed=%d ok=%d err=%d dead=%d files=%d "
                "(%.1f alb/s, ETA %.1fh)",
                processed, total["ok"], total["error"], total["dead"],
                total["files"], rate, eta_h,
            )
            if total["error"] and total["ok"] == 0 and batch["error"] == take:
                # Whole batch failed (likely global block); stop to avoid
                # hammering. Rerun resumes from remaining queue.
                total["stopped"] = "all_errors"
                break
    finally:
        con.close()
    return total


def run(db_path=None, ids: list[str] | None = None, **kwargs) -> dict:
    db_path = db_path or config.DB_PATH
    con = db.connect(db_path)
    try:
        db.init_db(con)
    finally:
        con.close()
    if ids:
        con = db.connect(db_path)
        try:
            ids = db.set_album_pending(con, ids)
        finally:
            con.close()
        # ids-path only honours worker-related knobs.
        crawl_kwargs = {k: kwargs[k] for k in ("workers", "min_interval") if k in kwargs}
        return asyncio.run(crawl_ids(db_path, ids, **crawl_kwargs))
    return asyncio.run(crawl_all(db_path, **kwargs))
