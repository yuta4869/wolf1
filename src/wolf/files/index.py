"""Local file index builder.

`build_index` walks a directory, applies include / exclude filters,
runs each candidate path through `ProjectBoundaryGuard` and
`SensitivePathGuard`, opens text files safely via `read_text_file`, and
records a small `FileIndexEntry` per file. The result is a `FileIndex`
that can be serialized to JSON via `save_index_json` / loaded via
`load_index_json`.

The index stores **metadata + a short snippet** per file, not the full
body, by design:

- A leaked index file must not be a leaked corpus.
- Search results re-read the file at query time to produce a snippet
  around the match, so the index does not need to hold the full text.
- `snippet` is the first ~160 bytes of the decoded file body, used only
  as a preview when no search match is available (e.g., listing).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .read_text import FileReadError, read_text_file
from ..safety.project_boundary import ProjectBoundaryGuard
from ..safety.sensitive_paths import SensitivePathGuard


DEFAULT_INDEX_DIR = ".wolf/index"
DEFAULT_INDEX_FILENAME = "files.json"
DEFAULT_INCLUDE_EXTS: Tuple[str, ...] = (".txt", ".md", ".rst", ".py")
DEFAULT_MAX_FILES = 500
DEFAULT_MAX_BYTES_PER_FILE = 1 * 1024 * 1024  # 1 MiB
DEFAULT_SNIPPET_BYTES = 160
INDEX_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FileIndexEntry:
    """Metadata recorded for one file. Path is repo-relative."""

    path: str
    size: int
    mtime: float
    extension: str
    snippet: str
    encoding: str


@dataclass(frozen=True)
class FileIndex:
    project_root: str
    created_at: float
    entries: Tuple[FileIndexEntry, ...]
    skipped: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = INDEX_SCHEMA_VERSION


@dataclass(frozen=True)
class IndexBuildResult:
    """Returned by build_index alongside the index, for CLI / audit use."""

    index: FileIndex
    accepted_count: int
    skipped_count: int


def _fnmatch_any(name: str, patterns: Sequence[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _candidate_files(
    *,
    root: Path,
    recursive: bool,
    include: Sequence[str],
    exclude: Sequence[str],
) -> List[Path]:
    if not root.exists() or not root.is_dir():
        return []
    walker = root.rglob("*") if recursive else root.glob("*")
    out: List[Path] = []
    for p in walker:
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if include and not (_fnmatch_any(rel, include) or _fnmatch_any(p.name, include)):
            continue
        if exclude and (_fnmatch_any(rel, exclude) or _fnmatch_any(p.name, exclude)):
            continue
        out.append(p)
    out.sort()
    return out


def _default_includes() -> Tuple[str, ...]:
    return tuple(f"*{ext}" for ext in DEFAULT_INCLUDE_EXTS)


def _make_snippet(text: str, *, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    blob = text.encode("utf-8")
    if len(blob) <= max_bytes:
        return text
    sliced = blob[:max_bytes]
    while sliced:
        try:
            return sliced.decode("utf-8")
        except UnicodeDecodeError:
            sliced = sliced[:-1]
    return ""


def build_index(
    *,
    project_root: Path,
    target_dir: Path,
    boundary: ProjectBoundaryGuard,
    sensitive: SensitivePathGuard,
    recursive: bool = True,
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    snippet_bytes: int = DEFAULT_SNIPPET_BYTES,
) -> IndexBuildResult:
    project_root = project_root.resolve()
    target_dir = target_dir.resolve()

    used_include = tuple(include) if include else _default_includes()
    used_exclude = tuple(exclude) if exclude else ()

    candidates = _candidate_files(
        root=target_dir,
        recursive=recursive,
        include=used_include,
        exclude=used_exclude,
    )

    entries: List[FileIndexEntry] = []
    skipped: List[str] = []

    for cand in candidates:
        if len(entries) >= max_files:
            skipped.append(f"max_files reached at {len(entries)}; remaining files skipped")
            break
        try:
            rel = str(cand.relative_to(project_root))
        except ValueError:
            skipped.append(f"{cand}: not under project_root")
            continue

        bd = boundary.check(cand)
        if not bd.allowed:
            skipped.append(f"{rel}: project_boundary ({bd.reason})")
            continue
        sd = sensitive.check(cand)
        if not sd.allowed:
            skipped.append(f"{rel}: sensitive_path ({sd.reason})")
            continue

        try:
            read_result = read_text_file(cand, max_bytes=max_bytes_per_file)
        except FileReadError as exc:
            skipped.append(f"{rel}: file_read ({exc.label})")
            continue

        try:
            stat = cand.stat()
            mtime = float(stat.st_mtime)
        except OSError as exc:
            skipped.append(f"{rel}: stat ({type(exc).__name__})")
            continue

        entries.append(
            FileIndexEntry(
                path=rel,
                size=read_result.byte_size,
                mtime=mtime,
                extension=cand.suffix.lower(),
                snippet=_make_snippet(read_result.text, max_bytes=snippet_bytes),
                encoding=read_result.encoding,
            )
        )

    index = FileIndex(
        project_root=str(project_root),
        created_at=time.time(),
        entries=tuple(entries),
        skipped=tuple(skipped),
        schema_version=INDEX_SCHEMA_VERSION,
    )
    return IndexBuildResult(
        index=index,
        accepted_count=len(entries),
        skipped_count=len(skipped),
    )


def default_index_path(project_root: Path) -> Path:
    return project_root / DEFAULT_INDEX_DIR / DEFAULT_INDEX_FILENAME


def save_index_json(index: FileIndex, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": index.schema_version,
        "project_root": index.project_root,
        "created_at": index.created_at,
        "entries": [asdict(e) for e in index.entries],
        "skipped": list(index.skipped),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def load_index_json(path: Path) -> FileIndex:
    if not path.exists():
        raise FileNotFoundError(f"index not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"index file is not a JSON object: {path}")
    version = int(payload.get("schema_version", 0))
    if version != INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"index schema_version {version} does not match expected "
            f"{INDEX_SCHEMA_VERSION}"
        )
    entries_raw = payload.get("entries", [])
    entries: List[FileIndexEntry] = []
    for raw in entries_raw:
        entries.append(
            FileIndexEntry(
                path=str(raw["path"]),
                size=int(raw["size"]),
                mtime=float(raw["mtime"]),
                extension=str(raw.get("extension", "")),
                snippet=str(raw.get("snippet", "")),
                encoding=str(raw.get("encoding", "utf-8")),
            )
        )
    return FileIndex(
        project_root=str(payload.get("project_root", "")),
        created_at=float(payload.get("created_at", 0.0)),
        entries=tuple(entries),
        skipped=tuple(payload.get("skipped", [])),
        schema_version=version,
    )
