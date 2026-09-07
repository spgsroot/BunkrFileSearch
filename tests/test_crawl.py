"""Crawler unit tests: domain picker, circuit breaker, crawl lock, and
per-album failure isolation in crawl_ids."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bunkr_index import crawl, db


class DomainPickerTest(unittest.TestCase):
    def test_sticks_to_preferred_until_failure(self) -> None:
        picker = crawl.DomainPicker(["a.example", "b.example"])
        self.assertEqual(picker.next(), "a.example")
        self.assertEqual(picker.next(), "a.example")
        picker.mark_failure("a.example")
        self.assertEqual(picker.next(), "b.example")
        # Failure of the fallback falls back to preferred anyway.
        picker.mark_failure("b.example")
        self.assertEqual(picker.next(), "a.example")

    def test_success_rewires_preference(self) -> None:
        picker = crawl.DomainPicker(["a.example", "b.example"])
        picker.mark_failure("a.example", seconds=0.01)
        picker.mark_success("b.example")
        import time

        time.sleep(0.02)
        self.assertEqual(picker.next(), "b.example")


class BreakerTest(unittest.TestCase):
    def test_trips_only_after_required_strikes_and_doubles(self) -> None:
        breaker = crawl.Breaker(base=10.0, cap=25.0, strikes=2)
        self.assertEqual(breaker.failure(), 0.0)
        self.assertEqual(breaker.failure(), 10.0)
        self.assertEqual(breaker.failure(), 0.0)
        self.assertEqual(breaker.failure(), 20.0)
        self.assertEqual(breaker.failure(), 0.0)
        self.assertEqual(breaker.failure(), 25.0)  # capped

    def test_success_resets(self) -> None:
        breaker = crawl.Breaker(base=10.0, strikes=1)
        self.assertEqual(breaker.failure(), 10.0)
        self.assertGreater(breaker.wait_seconds(), 0.0)
        breaker.ok()
        self.assertEqual(breaker.wait_seconds(), 0.0)
        self.assertEqual(breaker.failure(), 10.0)


class CrawlLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "crawl.lock"
        # Pin a /proc-style start-ticks identity so ownership checks are
        # deterministic on platforms without /proc (macOS).
        patches = [
            mock.patch.object(crawl, "LOCK_FILE", self.lock),
            mock.patch.object(crawl.config, "DATA_DIR", Path(self.tmp.name)),
            mock.patch.object(crawl, "_process_start_ticks", lambda pid: "1000"),
        ]
        self._patches = patches
        for patch in patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self.tmp.cleanup()

    def test_second_acquire_fails_and_release_frees(self) -> None:
        self.assertTrue(crawl.acquire_crawl_lock())
        self.assertFalse(crawl.acquire_crawl_lock())
        crawl.release_crawl_lock()
        self.assertTrue(crawl.acquire_crawl_lock())
        crawl.release_crawl_lock()

    def test_stale_lock_from_dead_pid_is_taken_over(self) -> None:
        # PIDs below our own that are not running: use an absurdly high one.
        self.lock.write_text("4194303")
        self.assertTrue(crawl.acquire_crawl_lock())
        crawl.release_crawl_lock()

    def test_release_does_not_remove_someone_elses_lock(self) -> None:
        self.assertTrue(crawl.acquire_crawl_lock())
        self.lock.write_text("999999:123")  # another owner rewrote it
        crawl.release_crawl_lock()
        self.assertTrue(self.lock.exists())


class CrawlIdsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "index.db")
        con = db.connect(self.db_path)
        db.init_db(con)
        con.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, ids, parsed_by_id):
        async def fake_fetch(client, picker, album_id):
            result = parsed_by_id[album_id]
            if isinstance(result, Exception):
                raise result
            return result

        # Production fetch order: ids always come from the queue, i.e. the
        # rows already exist (discovery or `add` created them).
        con = db.connect(self.db_path)
        try:
            db.set_album_pending(con, ids)
        finally:
            con.close()
        with mock.patch.object(crawl, "fetch_album", side_effect=fake_fetch):
            import asyncio

            return asyncio.run(
                crawl.crawl_ids(self.db_path, ids, workers=2, min_interval=0)
            )

    def test_ok_path_writes_files(self) -> None:
        parsed = {
            "title": "Album",
            "thumb": "",
            "files": [
                {"file_id": 1, "search_name": "a.jpg", "media": "Image",
                 "size": 1, "uploaded_at": None},
            ],
        }
        summary = self._run(["al1"], {"al1": ("ok", parsed, False)})
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["files"], 1)
        con = db.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM files WHERE album_id = 'al1'"
            ).fetchone()
            self.assertEqual(row["n"], 1)
        finally:
            con.close()

    def test_duplicate_file_ids_do_not_abort_the_wave(self) -> None:
        """Regression: one album whose page lists the same file id twice used
        to raise IntegrityError through gather and kill the whole wave."""
        good = {
            "title": "Good",
            "thumb": "",
            "files": [
                {"file_id": 1, "search_name": "ok.jpg", "media": "Image",
                 "size": 1, "uploaded_at": None},
            ],
        }
        dup = {
            "title": "Dup",
            "thumb": "",
            "files": [
                {"file_id": 7, "search_name": "x.jpg", "media": "Image",
                 "size": 1, "uploaded_at": None},
                {"file_id": 7, "search_name": "y.jpg", "media": "Image",
                 "size": 1, "uploaded_at": None},
            ],
        }
        summary = self._run(
            ["good", "dup"], {"good": ("ok", good, False), "dup": ("ok", dup, False)}
        )
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["error"], 1)
        self.assertIn(("dup", summary["errors"][0][1]), [("dup", e) for _, e in summary["errors"]])
        con = db.connect(self.db_path)
        try:
            good_row = con.execute(
                "SELECT indexed_at FROM albums WHERE bunkr_id = 'good'"
            ).fetchone()
            self.assertIsNotNone(good_row["indexed_at"])
            dup_row = con.execute(
                "SELECT attempts, last_error FROM albums WHERE bunkr_id = 'dup'"
            ).fetchone()
            self.assertGreaterEqual(dup_row["attempts"], 1)
            self.assertIn("db write", dup_row["last_error"])
        finally:
            con.close()

    def test_dead_and_error_paths(self) -> None:
        summary = self._run(
            ["gone", "err"],
            {"gone": ("dead", None, False), "err": ("error", "HTTP 500", True)},
        )
        self.assertEqual(summary["dead"], 1)
        self.assertEqual(summary["error"], 1)
        con = db.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT dead FROM albums WHERE bunkr_id = 'gone'"
            ).fetchone()
            self.assertEqual(row["dead"], 1)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
