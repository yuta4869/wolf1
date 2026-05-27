"""Group local mail into conversation threads.

The builder reads the standard threading headers exposed on
`ParsedMail` (Message-ID / In-Reply-To / References). When a parent
cannot be located through those headers, the builder falls back to
grouping by a normalized subject (the subject with leading
"Re:" / "Fwd:" / "FW:" / "AW:" stripped, lowercased, whitespace
collapsed).

The thread output never contains raw mail bodies; only metadata about
each message (subject, from, date, message_id) is included. Callers
who need to render a thread should look up the messages in the
original `ParsedMail` list by `ThreadMessage.index`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .read_local import ParsedMail


_REPLY_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw)\s*:\s*)+",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_subject(subject: str) -> str:
    if not subject:
        return ""
    s = _REPLY_PREFIX_RE.sub("", subject)
    s = _WHITESPACE_RE.sub(" ", s)
    return s.strip().lower()


@dataclass(frozen=True)
class ThreadMessage:
    """Per-message slot inside a Thread (no body bytes)."""

    index: int  # original ParsedMail index in the input sequence
    subject: str
    from_: str
    date: str
    message_id: str


@dataclass(frozen=True)
class Thread:
    thread_id: str
    subject: str
    message_count: int
    participants: Tuple[str, ...]
    first_date: str
    last_date: str
    messages: Tuple[ThreadMessage, ...]


def _hash_id(values: Sequence[str]) -> str:
    h = hashlib.sha256()
    for v in values:
        h.update(v.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return "subject:" + h.hexdigest()[:16]


def _representative_subject(messages: Sequence[ParsedMail]) -> str:
    if not messages:
        return ""
    first = messages[0]
    stripped = _REPLY_PREFIX_RE.sub("", first.subject).strip()
    return stripped or first.subject


def build_threads(messages: Sequence[ParsedMail]) -> List[Thread]:
    """Cluster `messages` into Thread objects.

    Strategy:
    1. Seed a union-find over per-message keys (Message-ID or a
       synthetic per-index key when missing).
    2. Union each message with every parent listed in its
       `in_reply_to` / `references` headers.
    3. Subject-bucket fallback: messages with the same normalized
       subject share a thread root.
    4. Emit a Thread per connected component, sorted by first_date.
    """
    if not messages:
        return []

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

    keys: List[str] = []
    for idx, pm in enumerate(messages):
        k = pm.message_id or f"_no_msgid_idx{idx}"
        parent.setdefault(k, k)
        keys.append(k)

    # Lineage links: In-Reply-To + References.
    for idx, pm in enumerate(messages):
        mine = keys[idx]
        for parent_id in list(pm.in_reply_to) + list(pm.references):
            if not parent_id:
                continue
            parent.setdefault(parent_id, parent_id)
            union(parent_id, mine)

    # Subject-bucket fallback.
    subject_buckets: Dict[str, str] = {}
    for pm, key in zip(messages, keys):
        norm = _normalize_subject(pm.subject)
        if not norm:
            continue
        if norm not in subject_buckets:
            subject_buckets[norm] = key
        else:
            union(subject_buckets[norm], key)

    groups: Dict[str, List[int]] = {}
    for idx, key in enumerate(keys):
        root = find(key)
        groups.setdefault(root, []).append(idx)

    threads: List[Thread] = []
    for idx_list in groups.values():
        # Skip phantom roots: a parent we added because of a lineage
        # link but for which we have no actual message.
        if not idx_list:
            continue
        msgs = [messages[i] for i in idx_list]
        sorted_pairs = sorted(
            zip(idx_list, msgs),
            key=lambda p: (p[1].date, p[1].message_id),
        )
        ordered_indices, ordered_msgs = zip(*sorted_pairs)
        thread_messages = tuple(
            ThreadMessage(
                index=oi,
                subject=om.subject,
                from_=om.from_,
                date=om.date,
                message_id=om.message_id,
            )
            for oi, om in zip(ordered_indices, ordered_msgs)
        )
        participants_seen: List[str] = []
        for pm in ordered_msgs:
            if pm.from_ and pm.from_ not in participants_seen:
                participants_seen.append(pm.from_)
        if any(pm.message_id for pm in ordered_msgs):
            thread_id = next(
                pm.message_id for pm in ordered_msgs if pm.message_id
            )
        else:
            thread_id = _hash_id([pm.subject for pm in ordered_msgs])
        threads.append(
            Thread(
                thread_id=thread_id,
                subject=_representative_subject(ordered_msgs),
                message_count=len(ordered_msgs),
                participants=tuple(participants_seen),
                first_date=ordered_msgs[0].date,
                last_date=ordered_msgs[-1].date,
                messages=thread_messages,
            )
        )

    threads.sort(key=lambda t: (t.first_date, t.thread_id))
    return threads
