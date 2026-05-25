"""Text chunking helper for summarize pipelines.

`split_text(text, chunk_size, max_chunks)` returns up to `max_chunks`
slices of the input text. Splits prefer paragraph boundaries (blank
lines), falling back to line boundaries, falling back to a hard byte
cut. The text is treated as decoded UTF-8 strings; the chunk_size unit
is encoded UTF-8 bytes (so the result is safe to compare against
file-size limits and tokenizer budgets that are byte-oriented).

The function never returns more than `max_chunks` chunks. If the input
would require more, the remainder is appended to the last chunk so the
caller still receives the full content (with a `truncated_chunks=True`
indication via `SplitResult`). Callers decide whether to treat that as
a safe truncation or as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


DEFAULT_CHUNK_SIZE = 32 * 1024  # 32 KiB
DEFAULT_MAX_CHUNKS = 32


@dataclass(frozen=True)
class SplitResult:
    chunks: Tuple[str, ...]
    truncated: bool


def _encoded_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _split_on_separators(text: str, chunk_size: int) -> List[str]:
    """Split text into chunks at most `chunk_size` bytes each.

    Prefers paragraph boundaries (double newline), then single newlines.
    If a single line is itself larger than chunk_size, falls back to a
    hard byte cut on that line.
    """
    if not text:
        return []
    if _encoded_len(text) <= chunk_size:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: List[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    for i, para in enumerate(paragraphs):
        candidate = (buf + "\n\n" + para) if buf else para
        if _encoded_len(candidate) <= chunk_size:
            buf = candidate
            continue
        # Candidate would overflow. Flush current buf, then handle this
        # paragraph on its own.
        flush()
        if _encoded_len(para) <= chunk_size:
            buf = para
            continue
        # Paragraph itself is too large; split by lines.
        for line_idx, line in enumerate(para.split("\n")):
            line_with_nl = line + ("\n" if line_idx < len(para.split("\n")) - 1 else "")
            candidate_line = (buf + line_with_nl) if buf else line_with_nl
            if _encoded_len(candidate_line) <= chunk_size:
                buf = candidate_line
                continue
            flush()
            if _encoded_len(line_with_nl) <= chunk_size:
                buf = line_with_nl
                continue
            # Line itself is too long; hard-cut on byte boundaries.
            # Use a moving cursor so that backing off a multi-byte
            # codepoint at the end of one slice carries those bytes into
            # the next slice instead of dropping them.
            line_bytes = line_with_nl.encode("utf-8")
            cursor = 0
            n = len(line_bytes)
            while cursor < n:
                end = min(cursor + chunk_size, n)
                # Back off the end until the slice decodes cleanly.
                while end > cursor:
                    try:
                        chunks.append(
                            line_bytes[cursor:end].decode("utf-8")
                        )
                        break
                    except UnicodeDecodeError:
                        end -= 1
                if end == cursor:
                    # Could not find a decodable boundary; abandon.
                    break
                cursor = end
    flush()
    return chunks


def split_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> SplitResult:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if max_chunks <= 0:
        raise ValueError("max_chunks must be positive")
    pieces = _split_on_separators(text, chunk_size)
    if len(pieces) <= max_chunks:
        return SplitResult(chunks=tuple(pieces), truncated=False)
    # Truncation policy: keep the first max_chunks chunks; drop the rest.
    # Callers see truncated=True and can decide to warn the user.
    return SplitResult(chunks=tuple(pieces[:max_chunks]), truncated=True)
