"""Tests for src/wolf/files/chunking.py."""

from __future__ import annotations

import unittest

from wolf.files.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_CHUNKS,
    SplitResult,
    split_text,
)


class SplitTextTest(unittest.TestCase):
    def test_empty_text_returns_empty_tuple(self) -> None:
        r = split_text("")
        self.assertEqual(r.chunks, ())
        self.assertFalse(r.truncated)

    def test_short_text_single_chunk(self) -> None:
        r = split_text("hello world\n")
        self.assertEqual(r.chunks, ("hello world\n",))
        self.assertFalse(r.truncated)

    def test_splits_on_paragraph_boundary(self) -> None:
        text = "para one is short.\n\npara two is also short.\n\npara three.\n"
        r = split_text(text, chunk_size=30)
        # Each paragraph is shorter than 30 bytes, but combining them
        # overflows, so each becomes its own chunk.
        self.assertEqual(len(r.chunks), 3)
        self.assertFalse(r.truncated)

    def test_splits_on_line_boundary_when_paragraph_too_large(self) -> None:
        text = (
            "line a is short\nline b is short\nline c is short\nline d short\n"
        )
        r = split_text(text, chunk_size=20)
        self.assertGreater(len(r.chunks), 1)
        self.assertFalse(r.truncated)

    def test_hard_cut_when_single_line_too_long(self) -> None:
        text = "x" * 100
        r = split_text(text, chunk_size=10)
        self.assertGreaterEqual(len(r.chunks), 10)
        # Each chunk is at most chunk_size bytes.
        for c in r.chunks:
            self.assertLessEqual(len(c.encode("utf-8")), 10)

    def test_truncates_at_max_chunks(self) -> None:
        text = "\n\n".join(f"para {i} content" for i in range(20))
        r = split_text(text, chunk_size=15, max_chunks=5)
        self.assertEqual(len(r.chunks), 5)
        self.assertTrue(r.truncated)

    def test_invalid_chunk_size_raises(self) -> None:
        with self.assertRaises(ValueError):
            split_text("x", chunk_size=0)
        with self.assertRaises(ValueError):
            split_text("x", chunk_size=-1)

    def test_invalid_max_chunks_raises(self) -> None:
        with self.assertRaises(ValueError):
            split_text("x", max_chunks=0)

    def test_japanese_multibyte_safe_cut(self) -> None:
        # Each Japanese character is 3 bytes in UTF-8. The hard-cut path
        # must back off if the byte-boundary falls in the middle of a
        # codepoint; otherwise decode would raise, and dropping the
        # backed-off bytes would silently lose characters.
        text = "あいうえお" * 50  # 750 bytes
        r = split_text(text, chunk_size=10, max_chunks=200)
        self.assertFalse(r.truncated)
        # Reassembling should round-trip.
        rejoined = "".join(r.chunks)
        self.assertEqual(rejoined, text)

    def test_defaults_match_constants(self) -> None:
        self.assertEqual(DEFAULT_CHUNK_SIZE, 32 * 1024)
        self.assertEqual(DEFAULT_MAX_CHUNKS, 32)

    def test_result_is_frozen_dataclass(self) -> None:
        r = split_text("hi")
        self.assertIsInstance(r, SplitResult)
        with self.assertRaises(Exception):
            r.chunks = ()  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
