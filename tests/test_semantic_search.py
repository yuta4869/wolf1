"""Tests for src/wolf/files/semantic_search.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wolf.fakes.embedding import FakeEmbeddingAdapter
from wolf.files.semantic_search import SemanticHit, search_semantic
from wolf.files.vector_index import VectorEntry, VectorIndex
from wolf.safety.project_boundary import ProjectBoundaryGuard
from wolf.safety.sensitive_paths import SensitivePathGuard


def _entry(path: str, embedding) -> VectorEntry:
    return VectorEntry(
        path=path,
        size=10,
        mtime=1.0,
        extension=".md",
        snippet=f"snippet of {path}",
        encoding="utf-8",
        embedding=tuple(embedding),
    )


class SemanticSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = Path(self.home_tmp.name).resolve()
        # Create real on-disk files so boundary / sensitive checks pass.
        (self.root / "a.md").write_text("apple banana\n", encoding="utf-8")
        (self.root / "b.md").write_text("zoo zebra\n", encoding="utf-8")
        (self.root / "c.md").write_text("apple cider\n", encoding="utf-8")
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "x.md").write_text(
            "apple secret\n", encoding="utf-8"
        )
        self.boundary = ProjectBoundaryGuard(self.root)
        self.sensitive = SensitivePathGuard(
            project_root=self.root, home=self.home
        )
        self.embedder = FakeEmbeddingAdapter(model="fake-embed")
        # Build vectors using the same fake so they are consistent.
        self.index = VectorIndex(
            project_root=str(self.root),
            created_at=1.0,
            embedding_model="fake-embed",
            dim=FakeEmbeddingAdapter.DIM,
            entries=(
                _entry("a.md", self.embedder.embed("apple banana")),
                _entry("b.md", self.embedder.embed("zoo zebra")),
                _entry("c.md", self.embedder.embed("apple cider")),
                _entry("secrets/x.md", self.embedder.embed("apple secret")),
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()
        self.home_tmp.cleanup()

    def test_returns_ranked_hits(self) -> None:
        hits = search_semantic(
            self.index,
            "apple",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
        )
        # secrets/x.md must be filtered by sensitive_path guard.
        paths = [h.path for h in hits]
        self.assertNotIn("secrets/x.md", paths)
        # Hits are sorted by score descending.
        scores = [h.score for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_is_in_minus_one_to_one(self) -> None:
        hits = search_semantic(
            self.index,
            "apple",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
        )
        for h in hits:
            self.assertGreaterEqual(h.score, -1.0001)
            self.assertLessEqual(h.score, 1.0001)

    def test_empty_query_returns_empty(self) -> None:
        hits = search_semantic(
            self.index,
            "",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
        )
        self.assertEqual(hits, [])

    def test_max_hits_cap(self) -> None:
        hits = search_semantic(
            self.index,
            "apple",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
            max_hits=1,
        )
        self.assertEqual(len(hits), 1)

    def test_sensitive_path_skipped(self) -> None:
        # Confirm secrets/x.md never appears even if it would rank top.
        hits = search_semantic(
            self.index,
            "apple secret",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
        )
        self.assertNotIn("secrets/x.md", [h.path for h in hits])

    def test_dataclass_shape(self) -> None:
        hits = search_semantic(
            self.index,
            "apple",
            embedder=self.embedder,
            project_root=self.root,
            boundary=self.boundary,
            sensitive=self.sensitive,
        )
        self.assertGreater(len(hits), 0)
        self.assertIsInstance(hits[0], SemanticHit)
        self.assertIn("snippet of", hits[0].snippet)


if __name__ == "__main__":
    unittest.main()
