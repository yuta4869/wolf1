"""Tests for src/wolf/files/vector_index.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wolf.files.vector_index import (
    VECTOR_INDEX_SCHEMA_VERSION,
    VectorEntry,
    VectorIndex,
    cosine_similarity,
    default_vector_index_path,
    load_vector_index_json,
    save_vector_index_json,
)


def _entry(path: str, emb) -> VectorEntry:
    return VectorEntry(
        path=path,
        size=10,
        mtime=1.0,
        extension=".md",
        snippet="snippet",
        encoding="utf-8",
        embedding=tuple(emb),
    )


class CosineTest(unittest.TestCase):
    def test_identical_is_one(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0)

    def test_orthogonal_is_zero(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_opposite_is_negative_one(self) -> None:
        self.assertAlmostEqual(
            cosine_similarity([1, 2, 3], [-1, -2, -3]), -1.0
        )

    def test_zero_vector_is_zero(self) -> None:
        self.assertEqual(cosine_similarity([0, 0, 0], [1, 2, 3]), 0.0)

    def test_unequal_length_is_zero(self) -> None:
        self.assertEqual(cosine_similarity([1, 2], [1, 2, 3]), 0.0)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(cosine_similarity([], []), 0.0)

    def test_ordering(self) -> None:
        # The closer vector to [1, 0] is [0.9, 0.1].
        q = [1.0, 0.0]
        a = [0.9, 0.1]
        b = [0.1, 0.9]
        self.assertGreater(cosine_similarity(q, a), cosine_similarity(q, b))


class RoundTripTest(unittest.TestCase):
    def test_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            idx = VectorIndex(
                project_root=str(root),
                created_at=1.0,
                embedding_model="fake-embed",
                dim=3,
                entries=(
                    _entry("a.md", [0.1, 0.2, 0.3]),
                    _entry("b.md", [0.0, 1.0, 0.0]),
                ),
                skipped=("c.md: skipped",),
            )
            out = root / ".wolf" / "index" / "embeddings.json"
            save_vector_index_json(idx, out)
            loaded = load_vector_index_json(out)
            self.assertEqual(loaded.schema_version, VECTOR_INDEX_SCHEMA_VERSION)
            self.assertEqual(loaded.embedding_model, "fake-embed")
            self.assertEqual(loaded.dim, 3)
            self.assertEqual([e.path for e in loaded.entries], ["a.md", "b.md"])
            self.assertEqual(list(loaded.entries[0].embedding), [0.1, 0.2, 0.3])
            self.assertEqual(loaded.skipped, ("c.md: skipped",))

    def test_load_missing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_vector_index_json(Path(tmp) / "no.json")

    def test_load_wrong_schema_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "wrong.json"
            p.write_text(
                json.dumps({"schema_version": 999, "entries": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_vector_index_json(p)

    def test_load_rejects_entry_without_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "entries": [
                            {
                                "path": "x.md",
                                "size": 1,
                                "mtime": 1.0,
                                "extension": ".md",
                                "snippet": "",
                                "encoding": "utf-8",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_vector_index_json(p)

    def test_default_path_under_dot_wolf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(
                default_vector_index_path(root).relative_to(root),
                Path(".wolf/index/embeddings.json"),
            )


if __name__ == "__main__":
    unittest.main()
