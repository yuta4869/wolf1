"""Substring search over parsed mail.

Searches subject / from / body (case-insensitive). Returns per-message
hits with a bounded snippet anchored at the match position. Raw full
bodies never leave this module — only the snippet does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .read_local import ParsedMail


DEFAULT_SNIPPET_CONTEXT = 80
DEFAULT_MAX_HITS = 10


@dataclass(frozen=True)
class MailHit:
    subject: str
    from_: str
    date: str
    message_id: str
    snippet: str
    match_field: str  # "subject" / "from" / "body"
    match_count: int
    has_attachments: bool = False
    attachments_count: int = 0


def _make_snippet(text: str, query: str, *, context_bytes: int) -> str:
    if not text:
        return ""
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx < 0:
        return ""
    blob = text.encode("utf-8")
    prefix_bytes = text[:idx].encode("utf-8")
    match_byte = len(prefix_bytes)
    start = max(0, match_byte - context_bytes)
    end = min(len(blob), match_byte + len(query.encode("utf-8")) + context_bytes)
    sliced = blob[start:end]
    while sliced:
        try:
            return sliced.decode("utf-8")
        except UnicodeDecodeError:
            sliced = sliced[:-1]
    return ""


def search_mail(
    messages: Sequence[ParsedMail],
    query: str,
    *,
    max_hits: int = DEFAULT_MAX_HITS,
    snippet_context_bytes: int = DEFAULT_SNIPPET_CONTEXT,
) -> List[MailHit]:
    if not query:
        return []
    needle = query.lower()
    hits: List[MailHit] = []
    for pm in messages:
        if len(hits) >= max_hits:
            break
        match_field = ""
        snippet = ""
        match_count = 0
        if needle in pm.subject.lower():
            match_field = "subject"
            snippet = pm.subject
            match_count = pm.subject.lower().count(needle)
        elif needle in pm.from_.lower():
            match_field = "from"
            snippet = pm.from_
            match_count = pm.from_.lower().count(needle)
        elif needle in pm.body.lower():
            match_field = "body"
            snippet = _make_snippet(
                pm.body, query, context_bytes=snippet_context_bytes
            )
            match_count = pm.body.lower().count(needle)
        if not match_field:
            continue
        hits.append(
            MailHit(
                subject=pm.subject,
                from_=pm.from_,
                date=pm.date,
                message_id=pm.message_id,
                snippet=snippet,
                match_field=match_field,
                match_count=match_count,
                has_attachments=pm.has_attachments,
                attachments_count=len(pm.attachments),
            )
        )
    return hits
