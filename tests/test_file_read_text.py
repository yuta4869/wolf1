"""Tests for src/wolf/files/read_text.py."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from wolf.files.read_text import (
    DEFAULT_MAX_BYTES,
    FileReadError,
    FileReadResult,
    read_text_file,
)


class _FileFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def cleanup(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, content, *, mode: str = "w") -> Path:
        p = self.root / name
        if mode == "w":
            p.write_text(content, encoding="utf-8")
        else:
            p.write_bytes(content)
        return p


class HappyPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_returns_file_read_result(self) -> None:
        p = self.fx.write("a.txt", "hello world\n")
        result = read_text_file(p)
        self.assertIsInstance(result, FileReadResult)
        self.assertEqual(result.text, "hello world\n")
        self.assertEqual(result.byte_size, len("hello world\n"))
        self.assertEqual(result.encoding, "utf-8")

    def test_handles_japanese_utf8(self) -> None:
        p = self.fx.write("ja.txt", "会議メモの要約\n")
        result = read_text_file(p)
        self.assertIn("会議メモ", result.text)

    def test_accepts_empty_file(self) -> None:
        p = self.fx.write("empty.txt", "")
        result = read_text_file(p)
        self.assertEqual(result.text, "")
        self.assertEqual(result.byte_size, 0)

    def test_accepts_string_path(self) -> None:
        p = self.fx.write("b.txt", "hi")
        # Passing a str (not Path) should also work.
        result = read_text_file(str(p))  # type: ignore[arg-type]
        self.assertEqual(result.text, "hi")


class MissingPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_none_path_raises(self) -> None:
        with self.assertRaises(FileReadError) as cm:
            read_text_file(None)  # type: ignore[arg-type]
        self.assertIn("None", cm.exception.label)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileReadError) as cm:
            read_text_file(self.fx.root / "does_not_exist.txt")
        self.assertIn("not found", cm.exception.label.lower())

    def test_directory_path_raises(self) -> None:
        with self.assertRaises(FileReadError) as cm:
            read_text_file(self.fx.root)
        self.assertIn("regular file", cm.exception.label.lower())


class SizeLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_file_exactly_at_limit_is_allowed(self) -> None:
        p = self.fx.write("ok.txt", "x" * 100)
        r = read_text_file(p, max_bytes=100)
        self.assertEqual(r.byte_size, 100)

    def test_file_above_limit_raises(self) -> None:
        p = self.fx.write("big.txt", "x" * 101)
        with self.assertRaises(FileReadError) as cm:
            read_text_file(p, max_bytes=100)
        self.assertIn("exceeds", cm.exception.label.lower())

    def test_default_limit_is_1mib(self) -> None:
        self.assertEqual(DEFAULT_MAX_BYTES, 1024 * 1024)


class BinaryDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_file_with_nul_bytes_rejected(self) -> None:
        p = self.fx.write("bin.dat", b"hello\x00world", mode="b")
        with self.assertRaises(FileReadError) as cm:
            read_text_file(p)
        self.assertIn("binary", cm.exception.label.lower())

    def test_file_with_high_control_byte_ratio_rejected(self) -> None:
        # Mostly control bytes (ESC, BEL, etc.) — looks like binary.
        blob = bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07] * 50)
        p = self.fx.write("ctrl.dat", blob, mode="b")
        with self.assertRaises(FileReadError):
            read_text_file(p)

    def test_japanese_utf8_with_high_bytes_accepted(self) -> None:
        # Japanese text is multi-byte UTF-8 with bytes 0x80-0xFF; must
        # NOT be classified as binary.
        text = "これはテキストファイルです。\n" * 50
        p = self.fx.write("ja_long.txt", text)
        r = read_text_file(p)
        self.assertIn("テキスト", r.text)


class DecodeErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_invalid_utf8_raises(self) -> None:
        # Invalid UTF-8 byte sequence (continuation byte without lead).
        p = self.fx.write("bad.txt", b"hello \xff\xfe world", mode="b")
        with self.assertRaises(FileReadError) as cm:
            read_text_file(p)
        # Could be caught by binary detection OR decode failure; both
        # are acceptable safe outcomes.
        label = cm.exception.label.lower()
        self.assertTrue(
            "decode" in label or "binary" in label,
            f"expected decode or binary error, got: {label}",
        )

    def test_unknown_encoding_raises(self) -> None:
        p = self.fx.write("a.txt", "hi")
        with self.assertRaises(FileReadError) as cm:
            read_text_file(p, encoding="not-a-real-encoding")
        self.assertIn("encoding", cm.exception.label.lower())


class ErrorPrivacyTest(unittest.TestCase):
    """FileReadError must not include file content in its representations."""

    def setUp(self) -> None:
        self.fx = _FileFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_oversize_error_does_not_leak_body(self) -> None:
        secret = "UNIQUE_FILE_BODY_SENTINEL_42_42_42"
        # File large enough to trigger size limit; body contains secret.
        content = secret + "\n" + "x" * 200
        p = self.fx.write("over.txt", content)
        try:
            read_text_file(p, max_bytes=50)
        except FileReadError as exc:
            rendered = repr(exc) + "|" + str(exc) + "|" + exc.label
            self.assertNotIn(secret, rendered)

    def test_binary_error_does_not_leak_body(self) -> None:
        secret = b"SECRET_HEADER_DATA_QQQQ"
        blob = secret + b"\x00" * 100
        p = self.fx.write("bin.dat", blob, mode="b")
        try:
            read_text_file(p)
        except FileReadError as exc:
            rendered = repr(exc) + "|" + str(exc) + "|" + exc.label
            self.assertNotIn("SECRET_HEADER_DATA_QQQQ", rendered)


class CharacteristicTest(unittest.TestCase):
    def test_module_does_not_import_third_party_http(self) -> None:
        # The reader is purely local; no urllib / requests / httpx needed.
        # Check import lines specifically (not docstring mentions).
        src = Path(__file__).resolve().parents[1] / "src" / "wolf" / "files" / "read_text.py"
        body = src.read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "httpx", "socket"):
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    self.assertNotIn(
                        forbidden,
                        stripped,
                        f"read_text.py should not import {forbidden!r}",
                    )


if __name__ == "__main__":
    unittest.main()
