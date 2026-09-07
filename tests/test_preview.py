from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bunkr_index import db, parse

_CARD = """
<div>
  <a href="https://bunkr.cr/a/XY123">
    <img src="/img/bunkr.svg" alt="" style="position:absolute">
    <img src="https://static.scdn.st/abc/thumbs/thumb-uuid.png"
         alt="Camilanga 1"
         class="thumb-img absolute inset-0 w-full h-full object-cover"
         onerror="this.remove()">
    <div>View album → Open</div>
    <div>15 files</div>
  </a>
  <a href="https://bunkr.cr/a/Z999">
    <img alt="No thumb album" class="thumb-img" src="https://cdn.example/x.png">
    <div>3 files</div>
  </a>
</div>
"""


class BalbumsThumbParseTest(unittest.TestCase):
    def test_parses_thumb_src_and_alt_title(self) -> None:
        cards = parse.parse_balbums_page(_CARD)
        self.assertEqual(len(cards), 2)
        first, second = cards
        self.assertEqual(first["bunkr_id"], "XY123")
        self.assertEqual(first["title"], "Camilanga 1")
        self.assertEqual(first["thumb"], "https://static.scdn.st/abc/thumbs/thumb-uuid.png")
        self.assertEqual(first["file_count"], 15)
        # Attribute order (alt before src) must not matter.
        self.assertEqual(second["thumb"], "https://cdn.example/x.png")
        self.assertEqual(second["title"], "No thumb album")


class ThumbPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(Path(self.tmp.name) / "index.db")
        db.init_db(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def _replace(self, bunkr_id: str, thumb: str) -> None:
        db.replace_album_files(
            self.con, bunkr_id, "Title", thumb,
            [{"file_id": 1, "search_name": "a.jpg", "media": "Image",
              "size": 1, "uploaded_at": None}],
        )

    def _thumb_of(self, bunkr_id: str) -> str:
        return self.con.execute(
            "SELECT thumb FROM albums WHERE bunkr_id = ?", (bunkr_id,)
        ).fetchone()["thumb"]

    def test_crawl_replace_stores_og_image_thumb(self) -> None:
        self._replace("al1", "https://cdn.example/preview.png")
        self.assertEqual(self._thumb_of("al1"), "https://cdn.example/preview.png")

    def test_crawl_replace_keeps_existing_thumb_when_absent(self) -> None:
        self._replace("al1", "https://cdn.example/preview.png")
        self._replace("al1", "")  # refreshed page without og:image
        self.assertEqual(self._thumb_of("al1"), "https://cdn.example/preview.png")

    def test_discovery_upsert_stores_thumb(self) -> None:
        now = "2024-01-01T00:00:00"
        db.upsert_discovered(self.con, [
            {"bunkr_id": "d1", "title": "New", "file_count": 5,
             "thumb": "https://static.scdn.st/d/th.png", "discovered_at": now},
        ])
        self.assertEqual(self._thumb_of("d1"), "https://static.scdn.st/d/th.png")


if __name__ == "__main__":
    unittest.main()
