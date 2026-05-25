"""Keyword search over a wolf FileIndex.

`search(index, query, ...)` returns a list of `SearchHit` objects, one
per matched file. The matcher is a case-insensitive substring search,
with the file's body re-read at query time so the snippet shown to the
user is anchored around the actual match position.

This module deliberately:
- does NOT use embeddings, BM25, or any external dependency,
- does NOT consult external network services,
- re-runs the path through the same `ProjectBoundaryGuard` /
  `SensitivePathGuard` instances as the indexer, so a file that was
  indexed earlier but has since become sensitive (e.g., moved under
  `secrets/`) is silently dropped from the result.

`FileIndexEntry.snippet` (the index-time preview) is used as a
fallback when the body re-read fails or the match cannot be located
in the freshly-read text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .index import FileIndex, FileIndexEntry
from .read_text import FileReadError, read_text_file
from ..safety.project_boundary import ProjectBoundaryGuard
from ..safety.sensitive_paths import SensitivePathGuard


DEFAULT_SNIPPET_CONTEXT = 60  # bytes of context before / after match
DEFAULT_MAX_HITS = 50


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: Optional[int]
    snippet: str
    match_count: int


def _make_match_snippet(text: str, query: str, *, context_bytes: int) -> str:
    """Find the first match in `text` and return surrounding context.

    Bytes are sliced; the result is decoded best-effort by backing off
    boundaries.
    """
    if not text or not query:
        return ""
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx < 0:
        return ""
    blob = text.encode("utf-8")
    # Map character index to approximate byte index by encoding the
    # prefix.
    prefix_bytes = text[:idx].encode("utf-8")
    match_byte = len(prefix_bytes)
    start = max(0, match_byte - context_bytes)
    end = min(len(blob), match_byte + len(query.encode("utf-8")) + context_bytes)
    sliced = blob[start:end]
    # Back off bytes at both ends until decode succeeds.
    while sliced:
        try:
            return sliced.decode("utf-8")
        except UnicodeDecodeError:
            # Trim one byte from whichever end is more likely to be the
            # broken codepoint. Cheap heuristic: trim the right first.
            sliced = sliced[:-1]
    return ""


def _line_number_of(text: str, query: str) -> Optional[int]:
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx < 0:
        return None
    return text[:idx].count("\n") + 1


def search(
    index: FileIndex,
    query: str,
    *,
    project_root: Path,
    boundary: ProjectBoundaryGuard,
    sensitive: SensitivePathGuard,
    max_hits: int = DEFAULT_MAX_HITS,
    snippet_context_bytes: int = DEFAULT_SNIPPET_CONTEXT,
    max_bytes_per_file: int = 1 * 1024 * 1024,
) -> List[SearchHit]:
    if not query:
        return []
    project_root = project_root.resolve()
    needle = query.lower()
    hits: List[SearchHit] = []
    for entry in index.entries:
        if len(hits) >= max_hits:
            break
        full = (project_root / entry.path).resolve()

        bd = boundary.check(full)
        if not bd.allowed:
            continue
        sd = sensitive.check(full)
        if not sd.allowed:
            continue

        try:
            read_result = read_text_file(full, max_bytes=max_bytes_per_file)
        except FileReadError:
            # Fall back to the index-time snippet for matching only.
            if needle in entry.snippet.lower():
                hits.append(
                    SearchHit(
                        path=entry.path,
                        line_number=None,
                        snippet=entry.snippet,
                        match_count=entry.snippet.lower().count(needle),
                    )
                )
            continue

        text_lower = read_result.text.lower()
        count = text_lower.count(needle)
        if count == 0:
            continue
        snippet = _make_match_snippet(
            read_result.text, query, context_bytes=snippet_context_bytes
        )
        if not snippet:
            snippet = entry.snippet
        hits.append(
            SearchHit(
                path=entry.path,
                line_number=_line_number_of(read_result.text, query),
                snippet=snippet,
                match_count=count,
            )
        )
    return hits
