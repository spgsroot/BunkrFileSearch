from __future__ import annotations

import time
import unittest

from bunkr_index.cache import TTLCache


class TTLCacheTest(unittest.TestCase):
    def test_get_set_roundtrip(self) -> None:
        cache = TTLCache(maxsize=4, ttl=60.0)
        cache.set("a", b"one")
        self.assertEqual(cache.get("a"), b"one")
        self.assertIsNone(cache.get("missing"))

    def test_expired_entry_is_removed(self) -> None:
        cache = TTLCache(maxsize=4, ttl=0.05)
        cache.set("a", b"one")
        time.sleep(0.08)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(len(cache), 0)

    def test_lru_eviction_drops_oldest(self) -> None:
        cache = TTLCache(maxsize=2, ttl=60.0)
        cache.set("a", b"1")
        cache.set("b", b"2")
        cache.get("a")  # refresh a, making b the least-recently used
        cache.set("c", b"3")
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), b"1")
        self.assertEqual(cache.get("c"), b"3")

    def test_value_overwrite_keeps_single_entry(self) -> None:
        cache = TTLCache(maxsize=4, ttl=60.0)
        cache.set("a", b"old")
        cache.set("a", b"new")
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache.get("a"), b"new")


if __name__ == "__main__":
    unittest.main()
