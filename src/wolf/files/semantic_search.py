"""Semantic search over a VectorIndex.

`search_semantic(index, query, embedder, ...)` returns a list of
`SemanticHit` ranked by cosine similarity. Each candidate path is
re-validated through `ProjectBoundaryGuard` and `SensitivePathGuard` at
query time, so a file that became sensitive after indexing is silently
dropped from results.

This module does NOT call the LLM (it only calls the embedder). It also
does not re-read files; the result uses the index-time snippet because
embedding inputs were bounded. Callers that need a longer match
context can pass the resulting paths to `read_text_file` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..adapters.embedding import EmbeddingAdapter
from ..safety.project_boundary import ProjectBoundaryGuard
from ..safety.sensitive_paths import SensitivePathGuard
from .vector_index import VectorIndex, cosine_similarity


DEFAULT_MAX_HITS = 5
DEFAULT_MIN_SCORE = 0.0


@dataclass(frozen=True)
class SemanticHit:
    path: str
    score: float
    snippet: str


def search_semantic(
    index: VectorIndex,
    query: str,
    *,
    embedder: EmbeddingAdapter,
    project_root: Path,
    boundary: ProjectBoundaryGuard,
    sensitive: SensitivePathGuard,
    max_hits: int = DEFAULT_MAX_HITS,
    min_score: float = DEFAULT_MIN_SCORE,
) -> List[SemanticHit]:
    if not query or not index.entries:
        return []
    query_vec = embedder.embed(query)
    if not query_vec:
        return []

    project_root = project_root.resolve()
    ranked: List[SemanticHit] = []
    for entry in index.entries:
        full = (project_root / entry.path).resolve()
        bd = boundary.check(full)
        if not bd.allowed:
            continue
        sd = sensitive.check(full)
        if not sd.allowed:
            continue
        score = cosine_similarity(list(query_vec), list(entry.embedding))
        if score < min_score:
            continue
        ranked.append(
            SemanticHit(path=entry.path, score=score, snippet=entry.snippet)
        )

    ranked.sort(key=lambda h: h.score, reverse=True)
    return ranked[:max_hits]
