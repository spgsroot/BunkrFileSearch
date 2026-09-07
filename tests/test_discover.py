"""Discovery crawler tests: ordered commits, stop conditions, error budget.

The network seam is `discover._fetch_page`; tests stub it with canned pages.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from bunkr_index import db, discover


def _page(cards: list[tuple[str, str, int]]) -> str:
    return "".join(
        f'<a class="card" href="https://bunkr.cr/a/{bid}">'
        f'<img class="thumb-img" src="x" alt="{title}">'
        f"<span>{count} files</span></a>"
        for bid, title, count in cards
    )


class DiscoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "index.db")
        con = db.connect(self.db_path)
        db.init_db(con)
        con.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, pages: dict[int, object], **kwargs) -> dict:
        async def fake_fetch(client, url):
            # Page number is the last query component.
            page = int(url.rsplit("page=", 1)[1])
            result = pages[page]
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(discover, "_fetch_page", side_effect=fake_fetch):
            return asyncio.run(
                discover.discover(self.db_path, delay=0, workers=2, **kwargs)
            )

    def test_commits_in_order_and_stops_on_empty_page(self) -> None:
        pages = {
            1: _page([("a1", "One", 1), ("a2", "Two", 2)]),
            2: _page([("a3", "Three", 3)]),
            3: "",  # empty -> stop
        }
        summary = self._run(pages)
        self.assertEqual(summary["stopped"], "empty_page")
        self.assertEqual(summary["new_albums"], 3)
        con = db.connect(self.db_path)
        try:
            ids = [
                r["bunkr_id"]
                for r in con.execute("SELECT bunkr_id FROM albums ORDER BY id")
            ]
            self.assertEqual(ids, ["a1", "a2", "a3"])
        finally:
            con.close()

    def test_stop_when_known(self) -> None:
        con = db.connect(self.db_path)
        try:
            db.upsert_discovered(
                con,
                [{"bunkr_id": "known1", "title": "K1", "file_count": 1, "thumb": ""}],
            )
        finally:
            con.close()
        pages = {
            1: _page([("known1", "K1", 1)]),  # all known -> stop
            2: _page([("never", "N", 1)]),   # must never be committed
        }
        summary = self._run(pages, stop_when_known=True)
        self.assertEqual(summary["stopped"], "all_known")
        con = db.connect(self.db_path)
        try:
            (total,) = con.execute("SELECT COUNT(*) FROM albums").fetchone()
            self.assertEqual(total, 1)
        finally:
            con.close()

    def test_error_budget_stops_after_repeated_failures(self) -> None:
        pages = {
            i: httpx.ConnectError("boom") for i in range(1, discover.MAX_PAGE_ERRORS + 1)
        }
        pages[discover.MAX_PAGE_ERRORS + 1] = _page([("a1", "One", 1)])
        summary = self._run(pages)
        self.assertEqual(summary["stopped"], "repeated_errors")
        self.assertEqual(summary["errors"], discover.MAX_PAGE_ERRORS)

    def test_single_failed_page_is_skipped(self) -> None:
        pages = {
            1: _page([("a1", "One", 1)]),
            2: httpx.HTTPStatusError(
                "503", request=mock.Mock(), response=mock.Mock()
            ),
            3: _page([("a3", "Three", 3)]),
            4: "",
        }
        summary = self._run(pages)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["new_albums"], 2)
        con = db.connect(self.db_path)
        try:
            ids = [r["bunkr_id"] for r in con.execute("SELECT bunkr_id FROM albums")]
            self.assertEqual(ids, ["a1", "a3"])
        finally:
            con.close()

    def test_max_pages_bound(self) -> None:
        pages = {i: _page([(f"p{i}", f"P{i}", 1)]) for i in range(1, 10)}
        summary = self._run(pages, max_pages=3)
        self.assertEqual(summary["stopped"], "max_pages")
        self.assertEqual(summary["pages"], 3)


if __name__ == "__main__":
    unittest.main()
