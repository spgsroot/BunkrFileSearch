"""Data-access tests: queue predicates, error marking, stats counters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bunkr_index import db


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(Path(self.tmp.name) / "index.db")
        db.init_db(self.con)

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()


class MarkErrorTest(DbTestCase):
    def test_dead_does_not_set_indexed_at(self) -> None:
        """Regression: marking an album dead used to stamp indexed_at, which
        leaked dead albums into `indexed_at IS NOT NULL` browse listings."""
        db.set_album_pending(self.con, ["al1"])
        db.mark_album_error(self.con, "al1", "HTTP 404", dead=True)
        row = self.con.execute(
            "SELECT dead, indexed_at, attempts FROM albums WHERE bunkr_id = 'al1'"
        ).fetchone()
        self.assertEqual(row["dead"], 1)
        self.assertIsNone(row["indexed_at"])
        self.assertEqual(row["attempts"], 1)

    def test_transient_error_increments_attempts_without_dead(self) -> None:
        db.set_album_pending(self.con, ["al1"])
        db.mark_album_error(self.con, "al1", "HTTP 429")
        row = self.con.execute(
            "SELECT dead, attempts FROM albums WHERE bunkr_id = 'al1'"
        ).fetchone()
        self.assertEqual(row["dead"], 0)
        self.assertEqual(row["attempts"], 1)


class PendingQueueTest(DbTestCase):
    def _seed(self) -> None:
        db.set_album_pending(self.con, ["fresh", "exhausted", "gone"])
        self.con.execute(
            "UPDATE albums SET attempts = 5 WHERE bunkr_id = 'exhausted'"
        )
        self.con.execute("UPDATE albums SET dead = 1 WHERE bunkr_id = 'gone'")

    def test_pending_excludes_exhausted_and_dead(self) -> None:
        self._seed()
        rows = db.pending_for_crawl(self.con)
        self.assertEqual([r["bunkr_id"] for r in rows], ["fresh"])

    def test_count_matches_queue(self) -> None:
        self._seed()
        self.assertEqual(db.count_pending_for_crawl(self.con), 1)
        self.assertEqual(
            db.count_pending_for_crawl(self.con),
            len(db.pending_for_crawl(self.con, limit=500)),
        )

    def test_stale_days_picks_old_indexed_albums(self) -> None:
        db.replace_album_files(
            self.con, "old", "Old", "",
            [{"file_id": 1, "search_name": "x.jpg", "media": "Image",
              "size": 1, "uploaded_at": None}],
        )
        self.con.execute(
            "UPDATE albums SET indexed_at = '2000-01-01T00:00:00' WHERE bunkr_id = 'old'"
        )
        self.assertEqual(db.count_pending_for_crawl(self.con), 0)
        self.assertEqual(db.count_pending_for_crawl(self.con, include_stale_days=7), 1)


class StatsTest(DbTestCase):
    def test_pending_and_stuck_split(self) -> None:
        """Regression: albums with attempts >= 5 were reported as pending even
        though nothing will ever crawl them."""
        db.set_album_pending(self.con, ["p1", "p2"])
        db.replace_album_files(
            self.con, "idx", "Indexed", "",
            [{"file_id": 1, "search_name": "x.jpg", "media": "Image",
              "size": 1, "uploaded_at": None}],
        )
        self.con.execute("UPDATE albums SET attempts = 5 WHERE bunkr_id = 'p2'")
        self.con.commit()
        s = db.stats(self.con)
        self.assertEqual(s["albums_total"], 3)
        self.assertEqual(s["albums_indexed"], 1)
        self.assertEqual(s["albums_pending"], 1)
        self.assertEqual(s["albums_stuck"], 1)


class UpsertDiscoveredTest(DbTestCase):
    def test_new_count_and_refresh(self) -> None:
        cards = [
            {"bunkr_id": "a1", "title": "One", "file_count": 10, "thumb": ""},
            {"bunkr_id": "a2", "title": "Two", "file_count": 20, "thumb": ""},
        ]
        self.assertEqual(db.upsert_discovered(self.con, cards), 2)
        # Re-upsert with a new title: known rows are refreshed, not counted new.
        cards[0]["title"] = "One v2"
        self.assertEqual(db.upsert_discovered(self.con, cards), 0)
        row = self.con.execute(
            "SELECT title FROM albums WHERE bunkr_id = 'a1'"
        ).fetchone()
        self.assertEqual(row["title"], "One v2")

    def test_empty_thumb_does_not_clobber_existing(self) -> None:
        db.upsert_discovered(
            self.con, [{"bunkr_id": "a1", "title": "T", "file_count": 1,
                        "thumb": "https://cdn/t.png"}],
        )
        db.upsert_discovered(
            self.con, [{"bunkr_id": "a1", "title": "T2", "file_count": 2, "thumb": ""}],
        )
        row = self.con.execute(
            "SELECT thumb FROM albums WHERE bunkr_id = 'a1'"
        ).fetchone()
        self.assertEqual(row["thumb"], "https://cdn/t.png")


class SetPendingTest(DbTestCase):
    def test_resets_dead_and_attempts_for_recrawl(self) -> None:
        db.set_album_pending(self.con, ["al1"])
        self.con.execute(
            "UPDATE albums SET attempts = 5, dead = 1 WHERE bunkr_id = 'al1'"
        )
        db.set_album_pending(self.con, ["al1"])
        row = self.con.execute(
            "SELECT attempts, dead, indexed_at FROM albums WHERE bunkr_id = 'al1'"
        ).fetchone()
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["dead"], 0)
        self.assertIsNone(row["indexed_at"])


if __name__ == "__main__":
    unittest.main()
