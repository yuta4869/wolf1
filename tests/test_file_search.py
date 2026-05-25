"""Tests for src/wolf/files/search.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wolf.files.index import build_index
from wolf.files.search import SearchHit, search
from wolf.safety.project_boundary import ProjectBoundaryGuard
from wolf.safety.sensitive_paths import SensitivePathGuard


class _SearchFixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = Path(self.home_tmp.name).resolve()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text(
            "Project plan.\nWe will summarize meeting notes.\nDone.\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "b.md").write_text(
            "Different topic.\nNo relevant keywords here.\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "c.txt").write_text(
            "Summarize the SUMMARIZE pipeline.\n",
            encoding="utf-8",
        )
        self.boundary = ProjectBoundaryGuard(self.root)
        self.sensitive = SensitivePathGuard(
            project_root=self.root, home=self.home
        )
        self.index = build_index(
            project_root=self.root,
            target_dir=self.root / "docs",
            boundary=self.boundary,
            sensitive=self.sensitive,
        ).index

    def cleanup(self) -> None:
        self.tmp.cleanup()
        self.home_tmp.cleanup()


class SearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _SearchFixture()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_returns_matching_files(self) -> None:
        hits = search(
            self.fx.index,
            "summarize",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        paths = {h.path for h in hits}
        self.assertIn("docs/a.md", paths)
        self.assertIn("docs/c.txt", paths)
        self.assertNotIn("docs/b.md", paths)

    def test_case_insensitive(self) -> None:
        hits = search(
            self.fx.index,
            "SUMMARIZE",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertGreater(len(hits), 0)

    def test_snippet_contains_match_context(self) -> None:
        hits = search(
            self.fx.index,
            "meeting notes",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertGreater(len(hits), 0)
        self.assertIn("meeting notes", hits[0].snippet.lower())

    def test_hits_count_matches(self) -> None:
        hits = search(
            self.fx.index,
            "summarize",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        # c.txt has the word twice (mixed case).
        c_hit = next(h for h in hits if h.path == "docs/c.txt")
        self.assertEqual(c_hit.match_count, 2)

    def test_hits_have_line_numbers(self) -> None:
        hits = search(
            self.fx.index,
            "Done",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertGreater(len(hits), 0)
        self.assertEqual(hits[0].line_number, 3)

    def test_empty_query_returns_empty(self) -> None:
        hits = search(
            self.fx.index,
            "",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertEqual(hits, [])

    def test_no_match_returns_empty(self) -> None:
        hits = search(
            self.fx.index,
            "zzzzz_no_such_token",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertEqual(hits, [])

    def test_max_hits_caps(self) -> None:
        hits = search(
            self.fx.index,
            "summarize",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
            max_hits=1,
        )
        self.assertEqual(len(hits), 1)

    def test_hit_dataclass_shape(self) -> None:
        hits = search(
            self.fx.index,
            "summarize",
            project_root=self.fx.root,
            boundary=self.fx.boundary,
            sensitive=self.fx.sensitive,
        )
        self.assertIsInstance(hits[0], SearchHit)


if __name__ == "__main__":
    unittest.main()
