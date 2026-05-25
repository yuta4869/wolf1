"""Tests for src/wolf/files/index.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wolf.files.index import (
    INDEX_SCHEMA_VERSION,
    FileIndex,
    FileIndexEntry,
    build_index,
    default_index_path,
    load_index_json,
    save_index_json,
)
from wolf.safety.project_boundary import ProjectBoundaryGuard
from wolf.safety.sensitive_paths import SensitivePathGuard


class _IndexFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = Path(self.home_tmp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text(
            "# A\nfirst note\n", encoding="utf-8"
        )
        (self.root / "docs" / "b.txt").write_text(
            "plain note about b\n", encoding="utf-8"
        )
        (self.root / "docs" / "code.py").write_text(
            "def foo():\n    return 1\n", encoding="utf-8"
        )
        (self.root / "docs" / "manual.rst").write_text(
            "Title\n=====\n\nbody\n", encoding="utf-8"
        )
        (self.root / "docs" / "image.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        )
        # Sensitive content under project root.
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "key.pem").write_text(
            "secret\n", encoding="utf-8"
        )
        (self.root / ".env").write_text("API_KEY=x\n", encoding="utf-8")
        self.boundary = ProjectBoundaryGuard(self.root)
        self.sensitive = SensitivePathGuard(
            project_root=self.root, home=self.home
        )

    def cleanup(self) -> None:
        self.tmp.cleanup()
        self.home_tmp.cleanup()


class BuildIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _IndexFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_indexes_default_extensions(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        paths = {e.path for e in result.index.entries}
        self.assertIn("docs/a.md", paths)
        self.assertIn("docs/b.txt", paths)
        self.assertIn("docs/code.py", paths)
        self.assertIn("docs/manual.rst", paths)
        # PNG must NOT be indexed (filtered out by include).
        self.assertNotIn("docs/image.png", paths)

    def test_walks_project_root_excluding_sensitive(self) -> None:
        # Include patterns broad enough to catch .env and secrets/*.
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            include=["*"],
        )
        paths = {e.path for e in result.index.entries}
        # secrets/key.pem and .env must be skipped, recorded in
        # result.index.skipped with reason "sensitive_path".
        self.assertNotIn("secrets/key.pem", paths)
        self.assertNotIn(".env", paths)
        joined = " | ".join(result.index.skipped)
        self.assertIn("sensitive_path", joined)
        self.assertIn(".env", joined)

    def test_binary_is_skipped(self) -> None:
        # Include broad pattern that picks up the .png.
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            include=["*"],
        )
        joined = " | ".join(result.index.skipped)
        self.assertIn("image.png", joined)
        self.assertIn("file_read", joined)

    def test_include_filter(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            include=["*.md"],
        )
        paths = {e.path for e in result.index.entries}
        self.assertEqual(paths, {"docs/a.md"})

    def test_exclude_filter(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            exclude=["b.txt"],
        )
        paths = {e.path for e in result.index.entries}
        self.assertNotIn("docs/b.txt", paths)
        self.assertIn("docs/a.md", paths)

    def test_max_files_caps_walk(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            max_files=2,
        )
        self.assertLessEqual(result.accepted_count, 2)
        joined = " | ".join(result.index.skipped)
        self.assertIn("max_files reached", joined)

    def test_entry_carries_metadata(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            include=["*.md"],
        )
        self.assertEqual(len(result.index.entries), 1)
        e = result.index.entries[0]
        self.assertEqual(e.path, "docs/a.md")
        self.assertGreater(e.size, 0)
        self.assertGreater(e.mtime, 0.0)
        self.assertEqual(e.extension, ".md")
        self.assertIn("first note", e.snippet)
        self.assertEqual(e.encoding, "utf-8")

    def test_snippet_is_bounded(self) -> None:
        sentinel = "UNIQUE_FILE_BODY_FOR_INDEX_TEST_4242"
        long_body = sentinel + ("\nfiller line " * 200)
        (self.fx.root / "docs" / "long.md").write_text(
            long_body, encoding="utf-8"
        )
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            include=["long.md"],
            snippet_bytes=64,
        )
        e = next(x for x in result.index.entries if x.path == "docs/long.md")
        # Snippet is capped at the requested byte budget. The full body
        # is much longer than 64 bytes; the snippet must be a strict
        # prefix.
        self.assertLessEqual(len(e.snippet.encode("utf-8")), 64)
        self.assertLess(len(e.snippet), len(long_body))
        # The size field records the full body length, not the snippet
        # length, so a leaked index still discloses size.
        self.assertGreater(e.size, len(e.snippet))


class JsonRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _IndexFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_save_load_round_trip(self) -> None:
        result = build_index(
            project_root=self.fx.root,
            target_dir=self.fx.root / "docs",
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        out = self.fx.root / ".wolf" / "index" / "files.json"
        save_index_json(result.index, out)
        self.assertTrue(out.exists())
        loaded = load_index_json(out)
        self.assertEqual(loaded.schema_version, INDEX_SCHEMA_VERSION)
        self.assertEqual(
            {e.path for e in loaded.entries},
            {e.path for e in result.index.entries},
        )

    def test_default_index_path_under_dot_wolf(self) -> None:
        p = default_index_path(self.fx.root)
        self.assertEqual(
            p.relative_to(self.fx.root), Path(".wolf/index/files.json")
        )

    def test_load_missing_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_index_json(self.fx.root / "does_not_exist.json")

    def test_load_wrong_schema_raises(self) -> None:
        p = self.fx.root / "wrong.json"
        p.write_text(
            json.dumps({"schema_version": 999, "entries": []}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_index_json(p)


if __name__ == "__main__":
    unittest.main()
