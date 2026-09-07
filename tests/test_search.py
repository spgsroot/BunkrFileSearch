from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bunkr_index import db, search


class SearchFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "index.db"
        self.con = db.connect(self.path)
        db.init_db(self.con)
        db.replace_album_files(
            self.con,
            "album",
            "Example album",
            "",
            [
                {
                    "file_id": 1,
                    "search_name": "Diora Baird Basic Instinct Audition.mp4",
                    "storage": "capture-raw-001.mp4",
                    "slug": "audition-clip",
                    "media": "Video",
                    "size": 10,
                    "uploaded_at": "2024-01-01T00:00:00",
                },
                {
                    "file_id": 2,
                    "search_name": "Alpha first.jpg",
                    "storage": "",
                    "slug": "",
                    "media": "Image",
                    "size": 20,
                    "uploaded_at": "2024-02-01T00:00:00",
                },
                {
                    "file_id": 3,
                    "search_name": "ALPHA latest.jpg",
                    "storage": "",
                    "slug": "",
                    "media": "Image",
                    "size": 30,
                    "uploaded_at": "2024-03-01T00:00:00",
                },
                {
                    "file_id": 4,
                    "search_name": "Alpha last.jpg",
                    "storage": "",
                    "slug": "",
                    "media": "Image",
                    "size": 40,
                    "uploaded_at": "2024-04-01T00:00:00",
                },
            ],
        )

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_schema_upgrades_single_column_fts(self) -> None:
        for trigger in ("files_ai", "files_ad", "files_au"):
            self.con.execute(f"DROP TRIGGER {trigger}")
        self.con.execute("DROP TABLE files_fts")
        self.con.execute(
            """
            CREATE VIRTUAL TABLE files_fts USING fts5(
                search_name, content='files', content_rowid='id', tokenize='trigram'
            )
            """
        )
        self.con.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        self.con.commit()

        db.init_db(self.con)

        columns = [row["name"] for row in self.con.execute("PRAGMA table_info(files_fts)")]
        self.assertEqual(columns, ["search_name", "storage", "slug"])
        result = search.search_files(self.con, q="capture-raw")
        self.assertEqual([row["file_id"] for row in result["results"]], [1])

    def test_schema_indexes_all_filename_aliases(self) -> None:
        columns = [row["name"] for row in self.con.execute("PRAGMA table_info(files_fts)")]
        self.assertEqual(columns, ["search_name", "storage", "slug"])

        result = search.search_files(self.con, q="capture-raw")
        self.assertEqual([row["file_id"] for row in result["results"]], [1])

    def test_multiword_and_exact_phrase_matching(self) -> None:
        words = search.search_files(self.con, q="Diora audition")
        phrase = search.search_files(self.con, q='"Baird Basic"')
        self.assertEqual([row["file_id"] for row in words["results"]], [1])
        self.assertEqual([row["file_id"] for row in phrase["results"]], [1])

    def test_mixed_separators_match_as_terms(self) -> None:
        self.con.execute(
            """
            INSERT INTO files(album_id, file_id, search_name, media, size, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("album", 5, "model-2025_final.jpg", "Image", 50, "2024-05-01T00:00:00"),
        )
        self.con.commit()
        result = search.search_files(self.con, q="model.2025-final")
        self.assertEqual([row["file_id"] for row in result["results"]], [5])

    def test_extension_filter_is_exact_and_indexed(self) -> None:
        result = search.search_files(self.con, extension="jpg")
        self.assertEqual([row["file_id"] for row in result["results"]], [4, 3, 2])
        self.assertTrue(all(row["extension"] == "jpg" for row in result["results"]))
        with self.assertRaises(ValueError):
            search.search_files(self.con, extension="jpg!")

    def test_prefix_sort_and_cursor_are_consistent(self) -> None:
        newest = search.search_files(self.con, q="al", sort="newest", limit=2)
        self.assertEqual([row["file_id"] for row in newest["results"]], [4, 3])
        self.assertTrue(newest["has_more"])
        self.assertIsNotNone(newest["next_cursor"])

        next_page = search.search_files(
            self.con, q="al", sort="newest", limit=2, cursor=newest["next_cursor"]
        )
        self.assertEqual([row["file_id"] for row in next_page["results"]], [2])
        self.assertFalse(next_page["has_more"])

    def test_prefix_query_uses_nocase_range_index(self) -> None:
        plan = self.con.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM files "
            "WHERE search_name COLLATE NOCASE LIKE ? ESCAPE '\\'",
            ("al%",),
        ).fetchall()
        self.assertIn("SEARCH", plan[0][3])


if __name__ == "__main__":
    unittest.main()
