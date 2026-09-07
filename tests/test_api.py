"""API tests via Litestar TestClient: caching, ETag, auth, validation.

The app's DB connection factory is monkeypatched to a per-test database, so
no test ever touches the real data/ directory.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from litestar.testing import TestClient

from bunkr_index import api, db


def _payload(album_id: str, name: str, media: str = "Image") -> dict:
    return {
        "file_id": 1,
        "search_name": name,
        "media": media,
        "size": 10,
        "uploaded_at": "2024-01-01T00:00:00",
    }


class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "index.db"
        con = db.connect(self.db_path)
        db.init_db(con)
        con.close()
        self._con_patch = mock.patch.object(
            api, "_con", lambda: db.connect(str(self.db_path))
        )
        self._con_patch.start()
        api._SEARCH_CACHE.clear()

    def tearDown(self) -> None:
        self._con_patch.stop()
        api._SEARCH_CACHE.clear()
        self.tmp.cleanup()

    def seed(self, albums: list[tuple[str, str]]) -> None:
        con = db.connect(self.db_path)
        try:
            for i, (album_id, name) in enumerate(albums):
                db.replace_album_files(con, album_id, f"Album {album_id}", "",
                                       [{**_payload(album_id, name), "file_id": i + 1}])
        finally:
            con.close()


class SearchEndpointTest(ApiTestCase):
    def test_search_roundtrip_and_ttl_cache(self) -> None:
        self.seed([("al1", "alpha outing.jpg")])
        with TestClient(api.app) as client:
            r1 = client.get("/api/search", params={"q": "alpha"})
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.headers["x-cache"], "miss")
            self.assertEqual(r1.json()["total"], 1)
            self.assertIn("took_ms", r1.json())
            r2 = client.get("/api/search", params={"q": "alpha"})
            self.assertEqual(r2.headers["x-cache"], "hit")
            self.assertEqual(r2.json()["total"], 1)

    def test_took_ms_reflects_build_time(self) -> None:
        """Regression: took_ms used to be computed before the query ran and
        was always ~0."""
        self.seed([("al1", "alpha outing.jpg")])
        real_search = api.search.search_files

        def slow_search(con, **kwargs):
            time.sleep(0.05)
            return real_search(con, **kwargs)

        with mock.patch.object(api.search, "search_files", slow_search):
            with TestClient(api.app) as client:
                r = client.get("/api/search", params={"q": "alpha"})
        self.assertGreaterEqual(r.json()["took_ms"], 40)

    def test_etag_and_304(self) -> None:
        self.seed([("al1", "alpha outing.jpg")])
        with TestClient(api.app) as client:
            r1 = client.get("/api/search", params={"q": "alpha"})
            etag = r1.headers["etag"]
            r2 = client.get(
                "/api/search", params={"q": "alpha"},
                headers={"if-none-match": etag},
            )
            self.assertEqual(r2.status_code, 304)
            self.assertEqual(r2.content, b"")

    def test_invalid_cursor_is_400(self) -> None:
        with TestClient(api.app) as client:
            r = client.get("/api/search", params={"q": "alpha", "cursor": "!!!"})
            self.assertEqual(r.status_code, 400)

    def test_cursor_context_mismatch_is_400(self) -> None:
        self.seed([("al1", "alpha one.jpg"), ("al2", "alpha two.jpg")])
        with TestClient(api.app) as client:
            r1 = client.get("/api/search", params={"q": "alpha", "per": 1})
            cursor = r1.json()["next_cursor"]
            r2 = client.get("/api/search", params={"q": "beta", "cursor": cursor})
            self.assertEqual(r2.status_code, 400)


class AlbumsEndpointTest(ApiTestCase):
    def test_browse_excludes_dead_albums(self) -> None:
        """Regression: dead albums used to leak into the indexed browse list
        (both results and total) because marking dead stamped indexed_at."""
        self.seed([("live1", "a.jpg")])
        con = db.connect(self.db_path)
        try:
            db.mark_album_error(con, "live1", "gone", dead=True)
        finally:
            con.close()
        with TestClient(api.app) as client:
            r = client.get("/api/albums", params={"indexed": "true"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["total"], 0)
            self.assertEqual(r.json()["results"], [])
            r2 = client.get("/api/albums")
            self.assertEqual(r2.json()["total"], 0)

    def test_album_title_search(self) -> None:
        self.seed([("al1", "f.jpg")])
        with TestClient(api.app) as client:
            r = client.get("/api/albums", params={"q": "al1"})  # title fallback
            self.assertEqual(r.status_code, 200)
            self.assertTrue(
                any(row["bunkr_id"] == "al1" for row in r.json()["results"])
            )


class MutationEndpointTest(ApiTestCase):
    def test_post_enqueues_and_delete_removes(self) -> None:
        with TestClient(api.app) as client:
            r = client.post(
                "/api/albums", json={"urls": ["https://bunkr.cr/a/AbC123"]}
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["accepted"], ["AbC123"])

            r = client.delete("/api/albums/AbC123")
            self.assertEqual(r.status_code, 200)
            r = client.delete("/api/albums/AbC123")
            self.assertEqual(r.status_code, 404)

    def test_post_validates_payload(self) -> None:
        with TestClient(api.app) as client:
            self.assertEqual(client.post("/api/albums", json={}).status_code, 400)
            self.assertEqual(
                client.post("/api/albums", json={"urls": ["no-id-here"]}).status_code,
                400,
            )

    def test_mutations_clear_the_cache(self) -> None:
        self.seed([("al1", "alpha.jpg")])
        with TestClient(api.app) as client:
            r1 = client.get("/api/albums")
            self.assertEqual(r1.headers["x-cache"], "miss")
            r2 = client.get("/api/albums")
            self.assertEqual(r2.headers["x-cache"], "hit")
            client.delete("/api/albums/al1")
            r3 = client.get("/api/albums")
            self.assertEqual(r3.headers["x-cache"], "miss")
            self.assertEqual(r3.json()["total"], 0)


class AdminAuthTest(ApiTestCase):
    def test_mutations_require_token_when_configured(self) -> None:
        with mock.patch.object(api.config, "ADMIN_TOKEN", "secret-token"):
            with TestClient(api.app) as client:
                r = client.post("/api/albums", json={"urls": ["https://bunkr.cr/a/AbC123"]})
                self.assertEqual(r.status_code, 401)
                r = client.delete("/api/albums/AbC123")
                self.assertEqual(r.status_code, 401)
                # Bearer header.
                r = client.post(
                    "/api/albums",
                    json={"urls": ["https://bunkr.cr/a/AbC123"]},
                    headers={"Authorization": "Bearer secret-token"},
                )
                self.assertEqual(r.status_code, 200)
                # X-Admin-Token header.
                r = client.delete(
                    "/api/albums/AbC123", headers={"X-Admin-Token": "secret-token"}
                )
                self.assertEqual(r.status_code, 200)
                # Read endpoints stay open.
                r = client.get("/api/stats")
                self.assertEqual(r.status_code, 200)

    def test_open_when_no_token_configured(self) -> None:
        with mock.patch.object(api.config, "ADMIN_TOKEN", ""):
            with TestClient(api.app) as client:
                r = client.post("/api/albums", json={"urls": ["https://bunkr.cr/a/AbC123"]})
                self.assertEqual(r.status_code, 200)


class StatsEndpointTest(ApiTestCase):
    def test_stats_counters(self) -> None:
        self.seed([("al1", "a.jpg")])
        with TestClient(api.app) as client:
            r = client.get("/api/stats")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["albums_total"], 1)
            self.assertEqual(body["albums_indexed"], 1)
            self.assertEqual(body["files"], 1)
            self.assertIn("albums_stuck", body)
            self.assertGreater(body["db_bytes"], 0)

    def test_security_headers_present(self) -> None:
        with TestClient(api.app) as client:
            r = client.get("/api/stats")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertIn("default-src 'self'", r.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
