# Bunkr File Search

A self-hosted search service for **filenames inside Bunkr albums**. It indexes
metadata only; media files are never downloaded.

- One request per album (`/<domain>/a/<id>?advanced=1`) yields the full file
  list, including `original`, `slug`, `size`, `type`, and `timestamp`. This was
  verified with albums containing up to 1,463 files.
- Album discovery uses the [balbums.st](https://balbums.st) directory
  (approximately 398,000 albums across 3,984 pages of 100 records).
- Storage is SQLite with FTS5 **trigram** indexes, so search is performed over
  filename substrings rather than whole terms only.

## Stack

Python 3.10+ managed with [uv](https://docs.astral.sh/uv/): Litestar,
Uvicorn, httpx for the async crawler, and the Python standard-library SQLite
module with WAL and FTS5.

## Install and run

```bash
uv sync                                   # create .venv and install dependencies
uv run bunkr-index init                   # create data/bunkr.db and its schema
uv run bunkr-index sync --workers 6       # terminal 1: discovery plus crawling
# terminal 2:
uv run bunkr-index serve --port 8000      # UI at http://127.0.0.1:8000
```

The first full run fills the database with approximately 398,000 album records
through `discover`, then fetches their metadata through `crawl`. Both commands
are idempotent and resume-safe: an interrupted run can be started again without
breaking the database.

## Commands

| Command | Purpose |
|---|---|
| `uv run bunkr-index init` | Create or update the schema and indexes. |
| `uv run bunkr-index discover` | Crawl the complete album directory. |
| `uv run bunkr-index discover --stop-known` | Stop when a page contains only known albums; useful for incremental refreshes. |
| `uv run bunkr-index discover --start-page N --pages M` | Crawl a bounded or resumable range of directory pages. |
| `uv run bunkr-index discover --workers N --delay S` | Control discovery concurrency and request spacing; pages are still committed in order. |
| `uv run bunkr-index crawl` | Index pending or previously unindexed albums. |
| `uv run bunkr-index crawl --ids <id>...` | Index specific album IDs or album URLs. |
| `uv run bunkr-index crawl --limit N --workers W --min-interval S` | Limit one run and control crawler concurrency and request rate. |
| `uv run bunkr-index crawl --stale-days N` | Also refresh albums last indexed more than `N` days ago. |
| `uv run bunkr-index sync` | Run incremental discovery followed by crawling every 24 hours. |
| `uv run bunkr-index sync --once` | Run one discovery-and-crawl cycle, then exit. |
| `uv run bunkr-index sync --stale-days N` | Refresh stale albums during every sync cycle. |
| `uv run bunkr-index sync --full-discover` | Scan every directory page instead of stopping at known records. |
| `uv run bunkr-index add <url-or-id>...` | Enqueue albums manually. |
| `uv run bunkr-index serve --host 127.0.0.1 --port 8000` | Run the search API and UI only; it never starts network crawling. |
| `uv run bunkr-index albums --q text` | Search known album titles from the command line. |
| `uv run bunkr-index stats` | Print database counters. |

## Search behavior

- **Queries of three or more characters** use FTS5 trigram substring search:
  `ali_smi`, `alicesmith`, and `name_001` work as expected. Spaces are treated
  as interchangeable with `_`, `-`, or no separator, so `cropcenter 1080` also
  finds `cropcenter_1080_1080.jpg`. Default ordering is BM25 relevance.
- **One- or two-character queries** use an indexed prefix match. For example,
  `sn` finds `SNOW_…`, but not `…_sn_…`. Trigram indexes cannot efficiently
  search substrings shorter than three characters, and a full scan of millions
  of records is deliberately avoided.
- **An empty query** lists the newest files.
- Media filtering accepts `image`, `video`, `audio`, or `other`; sort orders are
  `relevance`, `newest`, `oldest`, and `size`.
- Match counting is capped at 50,000. Above the cap the API returns
  `total: 50000` with `truncated: true`, displayed as `50,000+` in the UI. This
  prevents expensive full counts for broad searches.

The UI also shows an **Album matches** section for albums whose titles match the
current query.

## API

| Method | URL | Description |
|---|---|---|
| GET | `/api/search?q=&media=&sort=&page=&per=` | Search files; returns `{total, truncated, results[], …}`. |
| GET | `/api/albums?q=&page=&per=&indexed=` | List or search albums. |
| GET | `/api/stats` | Return index counters. |
| POST | `/api/albums` with `{"urls": [...]}` | Enqueue albums for crawling. |
| DELETE | `/api/albums/{bunkr_id}` | Delete an album and its files. |
| GET | `/` | Serve the web UI. |

## Database schema

`data/bunkr.db` is ignored by Git.

```text
albums(bunkr_id PK, title, file_count, thumb, discovered_at, updated_at,
       indexed_at, attempts, last_error, dead)
files(id, album_id → albums, file_id, search_name, storage, slug, mime,
      media, size, uploaded_at)          UNIQUE(album_id, file_id)
files_fts   FTS5 trigram index over search_name (external content, triggers)
albums_fts  FTS5 trigram index over title
```

FTS indexes are maintained incrementally by triggers on every insert. New
metadata is produced by the separate `sync` process.

### SQLite runtime compatibility

FTS5 index files must be served by a compatible SQLite runtime. When moving a
database between systems with different SQLite versions, rebuild the external
content FTS indexes once using the target runtime before serving traffic:

```sql
INSERT INTO files_fts(files_fts) VALUES('rebuild');
INSERT INTO albums_fts(albums_fts) VALUES('rebuild');
```

Run this while the web service is stopped. It preserves the source records and
recreates only the two derived FTS indexes.

## Important indexes

- `files(search_name)` handles one- and two-character prefix search without a
  table scan.
- `files(uploaded_at DESC, id DESC)` serves newest-first sorting without a
  temporary sort, including deep pagination.
- FTS5 trigram supports indexed filename substring search.

## Separate sync and serve processes

The crawler and UI are independent processes sharing one SQLite database:

```text
Terminal 1: uv run bunkr-index sync --workers 6
              discover → crawl → write data/bunkr.db → repeat every 24 hours

Terminal 2: uv run bunkr-index serve --port 8000
              API and UI only → read the current database state
```

At startup, `serve` only validates or creates the SQLite schema. It makes no
requests to Bunkr or balbums.st and can be restarted independently. `sync`
holds `data/crawl.lock` during a complete cycle, preventing a manual `crawl`
from running at the same time. SQLite WAL lets the UI read while metadata is
being written.

By default, `sync` performs incremental discovery and drains the crawl queue.
Use `--stale-days N` to refresh already indexed albums periodically. Use
`--once` for a one-off cycle or `--full-discover` for a full directory pass.

Set `BUNKR_DB=<path>` to the same database path for both processes when using a
non-default location.

## Docker Compose

Run the crawler and web service with one shared persistent database:

```bash
docker compose up -d --build
```

The UI is available at `http://127.0.0.1:8000`. The database persists in the
local `data/` directory, mounted into both `sync` and `web` containers.

```bash
docker compose ps
docker compose logs -f sync
docker compose logs -f web
docker compose down
```

Environment variables:

- `PORT`: external web port.
- `SYNC_WORKERS`: crawler concurrency.
- `SYNC_MIN_INTERVAL`: minimum seconds between request starts.
- `SYNC_INTERVAL_HOURS`: hours to wait after a completed sync cycle; defaults to `24`.

## Scale and request politeness

- A full corpus of roughly 398,000 albums and 12 million files requires around
  4.5–6 GB in SQLite. Trigram search remains fast; crawling is network-bound.
- The crawler uses one Bunkr domain at a time and respects rate limits. With
  `--workers 6 --min-interval 0.15`, it processes roughly 5–7 albums per second
  and a full corpus takes about 16–19 hours.
- On `403`, `429`, `5xx`, or timeouts, the crawler rotates through fallback
  domains from `bunkr_index/config.py`, backing off each failed domain for 30
  seconds or the server-supplied `Retry-After` interval.
- A `404` marks an album as dead. Other failures, including `200` pages without
  a file list, are treated as transient: no empty index is written, attempts
  increase, and subsequent runs retry the album.
- If multiple albums rate-limit across every domain, a global circuit breaker
  pauses the crawler exponentially from 10 seconds up to 300 seconds, resetting
  after the first successful request.
- The selected domain remains sticky until a failure, avoiding unnecessary
  redirects and load on fallback domains.

## Project layout

```text
bunkr_index/
  config.py    domains, headers, paths, limits
  db.py        SQLite schema, FTS5 indexes, and data access
  parse.py     window.albumFiles and balbums.st card parsers
  discover.py  directory crawler with ordered commits
  crawl.py     album metadata workers, domain rotation, resume support
  sync.py      long-running discover → crawl process
  search.py    FTS queries, separator variants, prefix search, capped totals
  api.py       Litestar API and static UI
  cli.py       init | discover | crawl | sync | serve | add | albums | stats
frontend/index.html  minimal web UI
data/bunkr.db         working database, excluded from Git
```

## Local measurements

Measurements with SQLite 3.53 on the local development corpus:

| Operation | Result |
|---|---|
| FTS search over 33–35k files | 0.2–1.6 ms |
| Prefix search shorter than 3 characters | 0.8–2 ms, index range scan |
| Empty query at offset 20k | about 6 ms |
| Crawler throughput | about 5–7 albums/s over the network; about 2,000 file rows/s written |
| Refreshing an album with 1,463 files | about 103 ms in one transaction |
