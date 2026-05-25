"""Deterministic fake embedding adapter for tests.

`FakeEmbeddingAdapter.embed(text)` returns a small fixed-dimension
vector derived from the text's character distribution. Two texts that
share a lot of characters get similar vectors; two unrelated texts get
dissimilar vectors. The output is not a real semantic embedding but it
satisfies the EmbeddingAdapter Protocol and gives cosine similarity
some signal to rank on, which is enough for unit tests.

The fake is intentionally cheap and pure — no I/O, no randomness, no
dependencies. Hashes are mixed with the model name so different model
names produce different vector spaces (useful for negative tests).
"""

from __future__ import annotations

from typing import List


class FakeEmbeddingAdapter:
    DIM = 16

    def __init__(self, *, model: str = "fake-embed") -> None:
        self.model = model
        self.calls: List[str] = []

    def embed(self, text: str) -> List[float]:
        self.calls.append(text)
        vec = [0.0] * self.DIM
        # Per-character mixing: each char contributes to a few slots
        # based on its codepoint.
        model_offset = sum(ord(c) for c in self.model) % self.DIM
        for ch in text:
            cp = ord(ch)
            vec[cp % self.DIM] += 1.0
            vec[(cp + 3) % self.DIM] += 0.5
            vec[(cp + model_offset) % self.DIM] += 0.25
        # L2 normalize so cosine similarity reduces to dot product.
        norm = sum(x * x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec
