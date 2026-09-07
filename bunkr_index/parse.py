"""HTML/JS parsing for Bunkr album pages and balbums.st list pages.

Everything is metadata-only: no media bytes are ever requested.
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime

# Item key/value pairs inside the `window.albumFiles` JS object literal.
# Values are either double-quoted JS strings or bare integers.
_KV_RE = re.compile(
    r'\b(id|name|original|slug|type|extension|size|timestamp)\s*:\s*'
    r'(?:"((?:[^"\\]|\\.)*)"|(\d+))'
)
_ID_LINE_RE = re.compile(r"(?m)^\s*id\s*:\s*(\d+)\s*[,}]?\s*$")
_TS_FMT = "%H:%M:%S %d/%m/%Y"

_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="(.*?)"')
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.S)
_BLOB_START_RE = re.compile(r"window\.albumFiles\s*=\s*\[")

# Canonical album-id extraction. A Bunkr album id is alphanumeric; every
# ingestion boundary (CLI, HTTP API, parsers) must agree on that.
ALBUM_ID_RE = re.compile(r"/a/([A-Za-z0-9]+)")
_ALBUM_ID_FULL_RE = re.compile(r"[A-Za-z0-9]+")


def extract_album_id(raw: str) -> str | None:
    """Extract a Bunkr album id from an album URL or a bare id string.

    Returns None for input that is neither, so callers can reject garbage
    instead of persisting arbitrary text as an id."""
    match = ALBUM_ID_RE.search(raw)
    if match:
        return match.group(1)
    token = raw.strip()
    return token if _ALBUM_ID_FULL_RE.fullmatch(token) else None


def _js_array_slice(source: str, start: int) -> str | None:
    """Return the balanced ``[ ... ]`` literal beginning at ``start``.

    Quote- and escape-aware: a ``"];`` sequence inside a filename string must
    not terminate the blob early (that used to silently drop every file after
    it). Returns None when the array never closes (truncated page)."""
    depth = 0
    quote = ""
    escaped = False
    for i in range(start, len(source)):
        ch = source[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    return None

# balbums.st album cards are plain <a> tags whose href points at a Bunkr
# album (/a/<id>) and whose thumbnail <img class="thumb-img"> alt holds title.
_CARD_RE = re.compile(
    r'<a\b[^>]*href="([^"]*/a/([A-Za-z0-9]+))"[^>]*>(.*?)</a>', re.S
)
_THUMB_IMG_TAG_RE = re.compile(
    r'<img\b[^>]*\bclass="[^"]*thumb-img[^"]*"[^>]*>'
)
_COUNT_RE = re.compile(r"(\d+)\s+files?\b", re.I)


def decode_js_str(s: str) -> str:
    """Decode a JS double-quoted string value (best effort, JSON subset)."""
    s = s.replace("\\'", "'")
    try:
        return json.loads('"' + s + '"')
    except (ValueError, json.JSONDecodeError):
        return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_timestamp(s: str) -> str | None:
    """'HH:MM:SS DD/MM/YYYY' -> ISO 'YYYY-MM-DDTHH:MM:SS' (or None)."""
    try:
        dt = datetime.strptime(s.strip(), _TS_FMT)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_album_page(page_html: str) -> dict:
    """Extract {title, thumb, files} from a Bunkr album page (?advanced=1).

    `files` items: file_id, storage, original, slug, mime, media, size,
    uploaded_at (ISO), search_name (best filename for indexing).
    """
    title = ""
    m = _OG_TITLE_RE.search(page_html)
    if m:
        title = _html.unescape(m.group(1)).strip()
    if not title:
        m = _TITLE_TAG_RE.search(page_html)
        if m:
            title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            title = re.sub(r"\s*\|\s*.*$", "", title).strip()

    thumb = ""
    m = re.search(r'property="og:image"\s+content="(.*?)"', page_html)
    if m:
        thumb = _html.unescape(m.group(1)).strip()

    ms = _BLOB_START_RE.search(page_html)
    if not ms:
        return {"title": title, "thumb": thumb, "files": [], "blob": False,
                "parse_error": False}
    blob = _js_array_slice(page_html, ms.end() - 1)
    if blob is None:
        # Page cut off mid-array: treat like a missing blob but flag it.
        return {"title": title, "thumb": thumb, "files": [], "blob": False,
                "parse_error": True}

    # Anchor on item-start lines `id: <num>` and slice between them. This is
    # robust against braces/quotes appearing inside filename strings.
    anchors = list(_ID_LINE_RE.finditer(blob))
    parse_error = bool(blob.strip()) and not anchors and "id:" in blob
    files: list[dict] = []
    for i, am in enumerate(anchors):
        file_id = int(am.group(1))
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(blob)
        chunk = blob[am.start() : end]
        vals: dict[str, str | int] = {"id": file_id}
        for kv in _KV_RE.finditer(chunk):
            key, str_val, int_val = kv.group(1), kv.group(2), kv.group(3)
            vals[key] = decode_js_str(str_val) if str_val is not None else int(int_val)
        original = str(vals.get("original", "")).strip()
        storage = str(vals.get("name", "")).strip()
        slug = str(vals.get("slug", "")).strip()
        search_name = original or slug or storage
        files.append(
            {
                "file_id": file_id,
                "storage": storage,
                "original": original,
                "slug": slug,
                "mime": str(vals.get("type", "")).strip(),
                "media": str(vals.get("extension", "")).strip(),
                "size": int(vals.get("size") or 0),
                "uploaded_at": parse_timestamp(str(vals.get("timestamp", ""))),
                "search_name": search_name,
            }
        )
    return {"title": title, "thumb": thumb, "files": files, "blob": True,
            "parse_error": parse_error}


def parse_balbums_page(page_html: str) -> list[dict]:
    """Parse balbums.st album cards into {url, bunkr_id, title, thumb, file_count}."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in _CARD_RE.finditer(page_html):
        url, bunkr_id, body = m.group(1), m.group(2), m.group(3)
        if bunkr_id in seen:
            continue
        seen.add(bunkr_id)
        thumb = ""
        it = _THUMB_IMG_TAG_RE.search(body)
        img_tag = it.group(0) if it else ""
        if img_tag:
            sm = re.search(r'\bsrc="([^"]*)"', img_tag)
            if sm:
                thumb = _html.unescape(sm.group(1)).strip()
            am = re.search(r'\balt="([^"]*)"', img_tag)
            title = _html.unescape(am.group(1)).strip() if am else ""
        else:
            title = ""
        if not title:
            # Fallback: pull the visible title from the card text.
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
            text = _html.unescape(text)
            text = re.sub(r"^View album\s*", "", text)
            text = re.sub(r"\s*→\s*Open\s*$", "", text)
            text = _COUNT_RE.sub("", text).strip(" -")
            title = text
        counts = [int(c) for c in _COUNT_RE.findall(body)]
        count = counts[-1] if counts else 0
        out.append(
            {
                "url": url,
                "bunkr_id": bunkr_id,
                "title": title,
                "thumb": thumb,
                "file_count": count,
            }
        )
    return out
