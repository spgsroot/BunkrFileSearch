"""Static configuration: paths, domains, request headers, crawl defaults."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"

# Overridable via environment (BUNKR_DB, BUNKR_DISCOVER_BASE, ...).
DB_PATH = Path(os.environ.get("BUNKR_DB", str(DATA_DIR / "bunkr.db")))
DISCOVER_BASE = os.environ.get("BUNKR_DISCOVER_BASE", "https://balbums.st/")

# Active Bunkr album domains, ordered. First is the default; the rest are
# fallbacks tried when a request is challenged (403/429/5xx/timeout).
DOMAINS = [
    "bunkr.cr",
    "bunkr.si",
    "bunkr.sk",
    "bunkr.ci",
    "bunkr.fi",
    "bunkr.ph",
    "bunkr.pk",
    "bunkr.ps",
    "bunkr.ws",
    "bunkr.ac",
    "bunkr.black",
    "bunkr.red",
    "bunkr.media",
    "bunkr.site",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Album-page request timeout (seconds).
TIMEOUT = 25.0

# Default crawl concurrency (workers) and politeness floor between requests.
DEFAULT_WORKERS = 6
MIN_REQUEST_INTERVAL = 0.15


# Discovery default page size accepted by balbums.st (20 or 100 only).
BALBUMS_PER = 100

# Optional bearer token guarding mutating API endpoints (POST/DELETE
# /api/albums). Empty string = open API, intended for local-only serving.
ADMIN_TOKEN = os.environ.get("BUNKR_ADMIN_TOKEN", "")
