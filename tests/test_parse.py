"""Parser tests: the most fragile module (regex over third-party HTML/JS)."""

from __future__ import annotations

import unittest

from bunkr_index import parse


def _album_page(body: str, title: str = "Test Album") -> str:
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        f'</head><body><script>window.albumFiles = {body};</script></body></html>'
    )


class JsArraySliceTest(unittest.TestCase):
    def test_simple_array(self) -> None:
        self.assertEqual(parse._js_array_slice('[1, 2];', 0), "[1, 2]")

    def test_brackets_inside_strings_do_not_close(self) -> None:
        src = r'[{"name": "a];b[.mp4"}, {"name": "c}].mp4"}];  garbage'
        self.assertEqual(parse._js_array_slice(src, 0), src[: src.rindex("]") + 1])

    def test_escaped_quotes_and_backslashes(self) -> None:
        src = r'[{"name": "quo\"].mp4"}, {"name": "back\\]slash.mp4"}];'
        self.assertEqual(parse._js_array_slice(src, 0), src[:-1])

    def test_truncated_array_returns_none(self) -> None:
        self.assertIsNone(parse._js_array_slice('[{"id": 1,', 0))

    def test_nested_structures(self) -> None:
        src = '[[1, {"b": "x]{"}], 2];'
        self.assertEqual(parse._js_array_slice(src, 0), src[:-1])


class AlbumPageTest(unittest.TestCase):
    def test_files_with_bracket_sequences_in_names_are_not_dropped(self) -> None:
        """Regression: non-greedy regex used to truncate the blob at the first
        `"];` inside a filename, silently losing every file after it."""
        page = _album_page(
            """[
{
id: 1,
name: "a];b.mp4",
original: "evil];name.mp4",
slug: "s1",
type: "video/mp4",
size: 10
},
{
id: 2,
name: "second.mp4",
original: "second.mp4",
slug: "s2",
type: "video/mp4",
size: 5
}
]"""
        )
        parsed = parse.parse_album_page(page)
        self.assertTrue(parsed["blob"])
        self.assertFalse(parsed["parse_error"])
        self.assertEqual([f["file_id"] for f in parsed["files"]], [1, 2])
        self.assertEqual(parsed["files"][0]["original"], "evil];name.mp4")

    def test_all_fields_and_search_name_fallbacks(self) -> None:
        page = _album_page(
            """[
{
id: 42,
name: "stor-42.mp4",
original: "",
slug: "slug-42",
type: "video/mp4",
extension: "Video",
size: 1234,
timestamp: "10:20:30 25/12/2024"
}
]"""
        )
        parsed = parse.parse_album_page(page)
        self.assertEqual(len(parsed["files"]), 1)
        f = parsed["files"][0]
        self.assertEqual(f["file_id"], 42)
        self.assertEqual(f["storage"], "stor-42.mp4")
        # original empty -> slug is the best display name
        self.assertEqual(f["search_name"], "slug-42")
        self.assertEqual(f["mime"], "video/mp4")
        self.assertEqual(f["media"], "Video")
        self.assertEqual(f["size"], 1234)
        self.assertEqual(f["uploaded_at"], "2024-12-25T10:20:30")
        self.assertEqual(parsed["title"], "Test Album")

    def test_missing_blob_is_not_a_parse_error(self) -> None:
        parsed = parse.parse_album_page("<html><head></head><body></body></html>")
        self.assertFalse(parsed["blob"])
        self.assertFalse(parsed["parse_error"])
        self.assertEqual(parsed["files"], [])

    def test_empty_blob_is_valid(self) -> None:
        parsed = parse.parse_album_page(_album_page("[]"))
        self.assertTrue(parsed["blob"])
        self.assertFalse(parsed["parse_error"])
        self.assertEqual(parsed["files"], [])

    def test_blob_without_items_is_a_parse_error(self) -> None:
        parsed = parse.parse_album_page(_album_page('[garbage: true, id: 7]'))
        self.assertTrue(parsed["blob"])
        self.assertTrue(parsed["parse_error"])
        self.assertEqual(parsed["files"], [])

    def test_truncated_blob_is_flagged(self) -> None:
        page = (
            '<html><head></head><body><script>window.albumFiles = '
            '[{id: 1, name: "cutof'
        )
        parsed = parse.parse_album_page(page)
        self.assertFalse(parsed["blob"])
        self.assertTrue(parsed["parse_error"])

    def test_title_falls_back_to_title_tag(self) -> None:
        page = (
            "<html><head><title>My Album | Bunkr</title></head>"
            "<body><script>window.albumFiles = [];</script></body></html>"
        )
        parsed = parse.parse_album_page(page)
        self.assertEqual(parsed["title"], "My Album")


class TimestampTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse.parse_timestamp("01:02:03 31/01/2024"), "2024-01-31T01:02:03")

    def test_invalid_inputs_return_none(self) -> None:
        for bad in ("", "not a date", "2024-01-01", "99:99:99 99/99/9999"):
            self.assertIsNone(parse.parse_timestamp(bad))


class BalbumsPageTest(unittest.TestCase):
    def test_cards_parsed_and_deduplicated(self) -> None:
        html = """
        <a class="card" href="https://bunkr.cr/a/abc123">
          <img class="thumb-img" src="https://cdn/t.png" alt="My &amp; Album">
          <span>12 files</span>
        </a>
        <a class="card" href="https://bunkr.cr/a/abc123">
          <img class="thumb-img" src="https://cdn/t.png" alt="duplicate card">
          <span>12 files</span>
        </a>
        <a class="card" href="https://bunkr.si/a/zzz999">
          <img class="thumb-img" src="x" alt="">
          View album Plain Title 3 files → Open
        </a>
        """
        cards = parse.parse_balbums_page(html)
        self.assertEqual([c["bunkr_id"] for c in cards], ["abc123", "zzz999"])
        self.assertEqual(cards[0]["title"], "My & Album")
        self.assertEqual(cards[0]["file_count"], 12)
        self.assertEqual(cards[1]["title"], "Plain Title")
        self.assertEqual(cards[1]["file_count"], 3)

    def test_empty_page(self) -> None:
        self.assertEqual(parse.parse_balbums_page("<html></html>"), [])


class ExtractAlbumIdTest(unittest.TestCase):
    def test_url_and_bare_id(self) -> None:
        self.assertEqual(parse.extract_album_id("https://bunkr.cr/a/AbC123"), "AbC123")
        self.assertEqual(parse.extract_album_id("AbC123"), "AbC123")
        self.assertEqual(parse.extract_album_id("  AbC123  "), "AbC123")

    def test_garbage_rejected(self) -> None:
        for bad in ("", "not an id!!", "https://example.com/", "a]b", "../../etc"):
            self.assertIsNone(parse.extract_album_id(bad))


if __name__ == "__main__":
    unittest.main()
