"""Local JSON vector index.

A `VectorIndex` is a list of `VectorEntry` records, each holding the
embedding vector for one file, plus the same lightweight metadata
fields stored in `FileIndex` (path, size, mtime, extension, snippet).
The index is serialized to JSON via `save_vector_index_json` /
`load_vector_index_json`.

This is the storage layer. Building the index from an `EmbeddingAdapter`
is the indexer's job (see CLI cmd_index_files); searching it is
`semantic_search.search_semantic`. Cosine similarity is implemented in
pure Python — no numpy.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Tuple


DEFAULT_VECTOR_INDEX_DIR = ".wolf/index"
DEFAULT_VECTOR_INDEX_FILENAME = "embeddings.json"
VECTOR_INDEX_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class VectorEntry:
    path: str
    size: int
    mtime: float
    extension: str
    snippet: str
    encoding: str
    embedding: Tuple[float, ...]


@dataclass(frozen=True)
class VectorIndex:
    project_root: str
    created_at: float
    embedding_model: str
    dim: int
    entries: Tuple[VectorEntry, ...]
    skipped: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = VECTOR_INDEX_SCHEMA_VERSION


def default_vector_index_path(project_root: Path) -> Path:
    return project_root / DEFAULT_VECTOR_INDEX_DIR / DEFAULT_VECTOR_INDEX_FILENAME


def save_vector_index_json(index: VectorIndex, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": index.schema_version,
        "project_root": index.project_root,
        "created_at": index.created_at,
        "embedding_model": index.embedding_model,
        "dim": index.dim,
        "entries": [
            {
                "path": e.path,
                "size": e.size,
                "mtime": e.mtime,
                "extension": e.extension,
                "snippet": e.snippet,
                "encoding": e.encoding,
                "embedding": list(e.embedding),
            }
            for e in index.entries
        ],
        "skipped": list(index.skipped),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_vector_index_json(path: Path) -> VectorIndex:
    if not path.exists():
        raise FileNotFoundError(f"vector index not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"vector index is not a JSON object: {path}")
    version = int(payload.get("schema_version", 0))
    if version != VECTOR_INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"vector index schema_version {version} does not match "
            f"expected {VECTOR_INDEX_SCHEMA_VERSION}"
        )
    entries: List[VectorEntry] = []
    for raw in payload.get("entries", []):
        emb = raw.get("embedding")
        if not isinstance(emb, list) or not emb:
            raise ValueError(f"vector index entry missing embedding: {raw.get('path')!r}")
        entries.append(
            VectorEntry(
                path=str(raw["path"]),
                size=int(raw["size"]),
                mtime=float(raw["mtime"]),
                extension=str(raw.get("extension", "")),
                snippet=str(raw.get("snippet", "")),
                encoding=str(raw.get("encoding", "utf-8")),
                embedding=tuple(float(x) for x in emb),
            )
        )
    return VectorIndex(
        project_root=str(payload.get("project_root", "")),
        created_at=float(payload.get("created_at", 0.0)),
        embedding_model=str(payload.get("embedding_model", "")),
        dim=int(payload.get("dim", 0)),
        entries=tuple(entries),
        skipped=tuple(payload.get("skipped", [])),
        schema_version=version,
    )


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two vectors. Returns 0.0 if either is zero.

    Implementation is pure Python; safe for the small dimensions we use
    in tests (16) and for the ~1000-dim embeddings Ollama returns.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))
