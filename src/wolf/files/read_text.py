"""Safe local text-file reader.

`read_text_file(path, ...)` opens a file in binary mode, checks size and
content type, and decodes as UTF-8 (configurable). Errors are raised as
`FileReadError` with a short, content-free label. Callers (the CLI, the
Router) are responsible for separately wrapping the returned content in
`UntrustedText` before passing it to a model.

This module does NOT enforce project_root membership — that is
`ProjectBoundaryGuard`'s job and must be applied at the call site before
read_text_file is invoked. This module also does NOT consult the
sensitive-paths allowlist; that is `SensitivePathGuard`'s job. The
separation keeps each guard small and independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_ENCODING = "utf-8"

# Heuristic threshold: if more than this fraction of bytes are outside the
# common printable / control set, treat as binary. Conservative; UTF-8
# text mixed with CJK and emoji should still pass.
_BINARY_NONTEXT_RATIO = 0.30

# Bytes that are commonly found in text: printable ASCII, CR, LF, TAB,
# plus the high range (0x80-0xFF) which is part of multi-byte UTF-8
# sequences. We accept high bytes as "possibly text" and rely on the
# UTF-8 decode step to catch true binary noise.
_TEXTUAL_LOW_BYTES = frozenset(
    list(range(0x20, 0x7F)) + [0x09, 0x0A, 0x0D, 0x0C]  # printable + TAB/LF/CR/FF
)


class FileReadError(Exception):
    """Raised by read_text_file when the file cannot be safely read.

    The exception's str / repr deliberately contains no file content; only
    a short label and (optionally) the offending path. Callers must not
    extract the file body from this exception.
    """

    def __init__(self, label: str, *, path: Optional[Path] = None) -> None:
        super().__init__(label)
        self.label = label
        self.path = path

    def __repr__(self) -> str:
        path_repr = repr(str(self.path)) if self.path is not None else "None"
        return f"FileReadError(label={self.label!r}, path={path_repr})"


@dataclass(frozen=True)
class FileReadResult:
    """Returned by read_text_file on success.

    `text` is the decoded file content. `byte_size` is the file's
    on-disk size at read time; useful for audit metadata.
    """

    text: str
    byte_size: int
    encoding: str


def _looks_like_binary(blob: bytes) -> bool:
    if not blob:
        return False
    # Quick NUL check — most binary files contain NUL bytes; text does not.
    if b"\x00" in blob:
        return True
    nontext = 0
    for b in blob:
        if b in _TEXTUAL_LOW_BYTES:
            continue
        if 0x80 <= b <= 0xFF:
            # Possibly part of a UTF-8 multibyte sequence.
            continue
        nontext += 1
    return (nontext / len(blob)) > _BINARY_NONTEXT_RATIO


def read_text_file(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    encoding: str = DEFAULT_ENCODING,
) -> FileReadResult:
    """Read `path` as UTF-8 text with safety checks.

    Raises `FileReadError` if:
    - the path does not exist,
    - the path is not a regular file (e.g., directory, socket),
    - the file exceeds `max_bytes`,
    - the file appears binary (NUL bytes / high non-text ratio),
    - the bytes cannot be decoded with the requested encoding.

    Returns a `FileReadResult` on success.
    """
    if path is None:
        raise FileReadError("path is None")
    if not isinstance(path, Path):
        path = Path(path)

    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        raise FileReadError("file not found", path=path) from exc
    except OSError as exc:
        raise FileReadError(f"stat failed ({type(exc).__name__})", path=path) from exc

    if not path.is_file():
        raise FileReadError("not a regular file", path=path)

    size = stat.st_size
    if size > max_bytes:
        raise FileReadError(
            f"file size {size} bytes exceeds max_bytes {max_bytes}",
            path=path,
        )

    try:
        with path.open("rb") as f:
            blob = f.read(max_bytes + 1)
    except OSError as exc:
        raise FileReadError(
            f"read failed ({type(exc).__name__})",
            path=path,
        ) from exc

    if len(blob) > max_bytes:
        raise FileReadError(
            f"file size {len(blob)} bytes exceeds max_bytes {max_bytes}",
            path=path,
        )

    if _looks_like_binary(blob):
        raise FileReadError(
            "file appears to be binary (NUL bytes or non-text ratio)",
            path=path,
        )

    try:
        text = blob.decode(encoding)
    except UnicodeDecodeError as exc:
        raise FileReadError(
            f"decode failed ({encoding}): "
            f"position {exc.start}-{exc.end}, reason {exc.reason}",
            path=path,
        ) from exc
    except LookupError as exc:
        raise FileReadError(
            f"unknown encoding {encoding!r}",
            path=path,
        ) from exc

    return FileReadResult(text=text, byte_size=len(blob), encoding=encoding)
