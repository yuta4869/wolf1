"""Group Gmail messages into conversation threads.

Gmail already exposes a server-side `threadId` on every message, so
the canonical grouping is by `threadId`. When `threadId` is missing
or empty (synthetic / fake messages, partial data), we fall back to
the same lineage + normalized-subject strategy as `wolf.mail.thread`.

The thread output never contains raw mail bodies, attachment bytes,
or label ids; only metadata (subject, from, date, message_id,
rfc822_message_id) is included. Callers who need a body should
re-read the source message via `GmailClient.read(...)`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .types import GmailMessage


_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw)\s*:\s*)+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_RFC822_ID_RE = re.compile(r"<[^>]+>")


def _normalize_subject(subject: str) -> str:
    if not subject:
        return ""
    s = _REPLY_PREFIX_RE.sub("", subject)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip().lower()


def _representative_subject(messages: Sequence[GmailMessage]) -> str:
    if not messages:
        return ""
    first = messages[0]
    stripped = _REPLY_PREFIX_RE.sub("", first.subject).strip()
    return stripped or first.subject


def _hash_id(values: Sequence[str]) -> str:
    h = hashlib.sha256()
    for v in values:
        h.update(v.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return "gmail-thread:" + h.hexdigest()[:16]


@dataclass(frozen=True)
class GmailThreadMessage:
    """Per-message slot inside a GmailThread (no body bytes)."""

    index: int  # original GmailMessage index in the input sequence
    gmail_message_id: str
    rfc822_message_id: str
    subject: str
    from_: str
    date: str


@dataclass(frozen=True)
class GmailThread:
    thread_id: str
    subject: str
    message_count: int
    participants: Tuple[str, ...]
    first_date: str
    last_date: str
    messages: Tuple[GmailThreadMessage, ...]


def _extract_references(value: str) -> Tuple[str, ...]:
    if not value:
        return ()
    return tuple(_RFC822_ID_RE.findall(value))


def build_threads(messages: Sequence[GmailMessage]) -> List[GmailThread]:
    """Cluster `messages` into GmailThread objects.

    Strategy:
    1. Primary key: `threadId` from Gmail. Messages sharing a
       non-empty `thread_id` cluster directly.
    2. For messages with empty `thread_id`, fall back to a union-find
       over `rfc822_message_id` linked by lineage parsed out of the
       (rarely-populated for fake clients) ``References`` /
       ``In-Reply-To`` form embedded in headers we already
       extracted. As a final fallback, group by normalized subject.
    3. Emit a GmailThread per group, sorted by `first_date`.
    """
    if not messages:
        return []

    # Step 1: partition by non-empty threadId.
    by_thread_id: Dict[str, List[int]] = {}
    leftovers: List[int] = []
    for idx, m in enumerate(messages):
        if m.thread_id:
            by_thread_id.setdefault(m.thread_id, []).append(idx)
        else:
            leftovers.append(idx)

    # Step 2: union-find over leftovers via rfc822 ids + subject.
    parent: Dict[str, str] = {}

    def find(node: str) -> str:
        while parent.get(node, node) != node:
            parent[node] = parent.get(parent[node], parent[node])
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    leftover_keys: Dict[int, str] = {}
    for idx in leftovers:
        m = messages[idx]
        k = m.rfc822_message_id or f"_no_rfc_idx{idx}"
        parent.setdefault(k, k)
        leftover_keys[idx] = k

    # Lineage edges if any embedded in the rfc822_message_id header
    # (rare; mostly fake fixtures). Walk References from snippet?
    # We don't have parsed In-Reply-To for Gmail messages — the
    # client only extracts Message-ID. So lineage fallback here is
    # essentially a no-op unless callers populate it.

    # Normalized-subject fallback.
    subject_buckets: Dict[str, int] = {}
    for idx in leftovers:
        norm = _normalize_subject(messages[idx].subject)
        if not norm:
            continue
        if norm not in subject_buckets:
            subject_buckets[norm] = idx
        else:
            union(
                leftover_keys[subject_buckets[norm]],
                leftover_keys[idx],
            )

    leftover_groups: Dict[str, List[int]] = {}
    for idx in leftovers:
        root = find(leftover_keys[idx])
        leftover_groups.setdefault(root, []).append(idx)

    # Build threads.
    threads: List[GmailThread] = []

    def make_thread(idx_list: List[int]) -> GmailThread:
        msgs = [messages[i] for i in idx_list]
        sorted_pairs = sorted(
            zip(idx_list, msgs),
            key=lambda p: (p[1].date, p[1].message_id),
        )
        ordered_indices, ordered_msgs = zip(*sorted_pairs)
        thread_messages = tuple(
            GmailThreadMessage(
                index=oi,
                gmail_message_id=om.message_id,
                rfc822_message_id=om.rfc822_message_id,
                subject=om.subject,
                from_=om.from_,
                date=om.date,
            )
            for oi, om in zip(ordered_indices, ordered_msgs)
        )
        participants_seen: List[str] = []
        for om in ordered_msgs:
            if om.from_ and om.from_ not in participants_seen:
                participants_seen.append(om.from_)
        # Pick a stable thread id:
        # - If any message has a non-empty thread_id, use the first.
        # - Else use the first non-empty rfc822 id.
        # - Else hash subjects.
        thread_id = ""
        for om in ordered_msgs:
            if om.thread_id:
                thread_id = om.thread_id
                break
        if not thread_id:
            for om in ordered_msgs:
                if om.rfc822_message_id:
                    thread_id = om.rfc822_message_id
                    break
        if not thread_id:
            thread_id = _hash_id([om.subject for om in ordered_msgs])
        return GmailThread(
            thread_id=thread_id,
            subject=_representative_subject(ordered_msgs),
            message_count=len(ordered_msgs),
            participants=tuple(participants_seen),
            first_date=ordered_msgs[0].date,
            last_date=ordered_msgs[-1].date,
            messages=thread_messages,
        )

    for idx_list in by_thread_id.values():
        threads.append(make_thread(idx_list))
    for idx_list in leftover_groups.values():
        threads.append(make_thread(idx_list))

    threads.sort(key=lambda t: (t.first_date, t.thread_id))
    return threads
