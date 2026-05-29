"""In-memory fake Gmail client for tests and CLI smoke runs.

FakeGmailClient supports the same search / read / create_draft surface
as the real GmailClient but never contacts the network. There is
deliberately no `send` method — send is not implemented anywhere in
this codebase. Trying to call `.send(...)` raises AttributeError.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .client import GmailClientError
from .draft import build_reply_draft_raw
from .types import (
    GmailAttachmentMeta,
    GmailDraft,
    GmailMessage,
    GmailSearchHit,
)


def _default_messages() -> List[GmailMessage]:
    return [
        GmailMessage(
            message_id="msg_1",
            thread_id="thread_1",
            subject="Quarterly planning meeting",
            from_="Alice Example <alice@example.invalid>",
            to="me@example.invalid",
            cc="",
            date="Tue, 21 May 2026 09:00:00 +0900",
            rfc822_message_id="<fake-001@example.invalid>",
            snippet="Kicking off Q3 planning. Please block 90 minutes Thursday.",
            body_text=(
                "Hi team,\n\n"
                "Kicking off Q3 planning. Please block 90 minutes on Thursday.\n\n"
                "Alice"
            ),
            has_attachments=False,
            attachments=(),
            label_ids=("INBOX", "UNREAD"),
        ),
        GmailMessage(
            message_id="msg_2",
            thread_id="thread_1",
            subject="Re: Quarterly planning meeting",
            from_="Bob Example <bob@example.invalid>",
            to="alice@example.invalid",
            cc="",
            date="Tue, 21 May 2026 17:00:00 +0900",
            rfc822_message_id="<fake-002@example.invalid>",
            snippet="Booked. I'll bring Q2 metrics.",
            body_text=(
                "Booked. I'll bring the Q2 metrics so we can compare\n"
                "against the new targets.\n\nBob"
            ),
            has_attachments=False,
            attachments=(),
            label_ids=("INBOX",),
        ),
        GmailMessage(
            message_id="msg_3",
            thread_id="thread_2",
            subject="Office snacks restock",
            from_="Dora Example <dora@example.invalid>",
            to="me@example.invalid",
            cc="",
            date="Wed, 22 May 2026 08:00:00 +0900",
            rfc822_message_id="<fake-003@example.invalid>",
            snippet="Reminder to restock the snacks tomorrow.",
            body_text="Reminder to restock the snacks tomorrow.\n\nDora",
            has_attachments=True,
            attachments=(
                GmailAttachmentMeta(
                    filename="snack-list.pdf",
                    mime_type="application/pdf",
                    size_bytes=1234,
                ),
            ),
            label_ids=("INBOX",),
        ),
        GmailMessage(
            message_id="msg_4",
            thread_id="thread_2",
            subject="Re: Office snacks restock",
            from_="Eve Example <eve@example.invalid>",
            to="dora@example.invalid",
            cc="me@example.invalid",
            date="Wed, 22 May 2026 09:00:00 +0900",
            rfc822_message_id="<fake-004@example.invalid>",
            snippet="Same vendor as last time, or do we want to try the new place?",
            body_text=(
                "Same vendor as last time, or do we want to try the new "
                "place?\n\nEve"
            ),
            has_attachments=False,
            attachments=(),
            label_ids=("INBOX",),
        ),
        GmailMessage(
            message_id="msg_5",
            thread_id="thread_3",
            subject="Q3 prep — actions and meeting",
            from_="Alice Example <alice@example.invalid>",
            to="team@example.invalid",
            cc="",
            date="Thu, 23 May 2026 09:00:00 +0900",
            rfc822_message_id="<fake-005@example.invalid>",
            snippet="A few action items and a planning meeting.",
            body_text=(
                "Hi team,\n\n"
                "A few things to lock down:\n\n"
                "Action item: Send the Q3 numbers to Alice by 2026-06-10\n"
                "Action item: Confirm the budget freeze on 2026-06-12\n"
                "Due: 2026-06-15\n"
                "Meeting: Quarterly planning sync on 2026-06-18 14:00\n\n"
                "Thanks,\nAlice"
            ),
            has_attachments=False,
            attachments=(),
            label_ids=("INBOX",),
        ),
    ]


@dataclass
class FakeGmailClient:
    """Minimal in-memory fake. Pre-populated with three sample messages.

    Tests can pass their own messages via the constructor or via
    `set_messages(...)`. Drafts created via `create_draft` are stored
    in `self.drafts` and returned with sequential ids.
    """

    messages: List[GmailMessage] = field(default_factory=_default_messages)
    drafts: List[Dict[str, str]] = field(default_factory=list)
    _next_draft_seq: int = 1
    raise_on_search: Optional[GmailClientError] = None
    raise_on_read: Optional[GmailClientError] = None
    raise_on_create_draft: Optional[GmailClientError] = None
    raise_on_get_thread: Optional[GmailClientError] = None

    def set_messages(self, messages: List[GmailMessage]) -> None:
        self.messages = list(messages)

    def search(
        self,
        *,
        query: str,
        max_results: int = 10,
    ) -> Tuple[GmailSearchHit, ...]:
        if self.raise_on_search is not None:
            raise self.raise_on_search
        if not isinstance(query, str) or not query.strip():
            raise GmailClientError("search query must be a non-empty string")
        if max_results <= 0:
            raise GmailClientError("max_results must be positive")
        terms = [t for t in query.lower().split() if t]
        hits: List[GmailSearchHit] = []
        for m in self.messages:
            if not terms or _matches_any_field(m, terms):
                hits.append(
                    GmailSearchHit(
                        message_id=m.message_id,
                        thread_id=m.thread_id,
                    )
                )
                if len(hits) >= max_results:
                    break
        return tuple(hits)

    def read(self, *, message_id: str) -> GmailMessage:
        if self.raise_on_read is not None:
            raise self.raise_on_read
        if not isinstance(message_id, str) or not message_id.strip():
            raise GmailClientError("message_id must be a non-empty string")
        for m in self.messages:
            if m.message_id == message_id:
                return m
        raise GmailClientError(f"gmail:read: message id not found")

    def get_thread(self, *, thread_id: str) -> Tuple[GmailMessage, ...]:
        if self.raise_on_get_thread is not None:
            raise self.raise_on_get_thread
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise GmailClientError("thread_id must be a non-empty string")
        members = tuple(
            m for m in self.messages if m.thread_id == thread_id
        )
        if not members:
            raise GmailClientError("gmail:get_thread: thread id not found")
        return members

    def create_draft(
        self,
        *,
        to: str,
        source_subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
        thread_id: str = "",
    ) -> GmailDraft:
        if self.raise_on_create_draft is not None:
            raise self.raise_on_create_draft
        # Build the raw encoding to mirror what the real client does and
        # to surface any encoder errors here too.
        raw = build_reply_draft_raw(
            to=to,
            source_subject=source_subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
        draft_id = f"fake_draft_{self._next_draft_seq}"
        message_id = f"fake_drafted_msg_{self._next_draft_seq}"
        self._next_draft_seq += 1
        self.drafts.append(
            {
                "draft_id": draft_id,
                "message_id": message_id,
                "thread_id": thread_id,
                "to": to,
                "source_subject": source_subject,
                "raw_len": str(len(raw)),
                "in_reply_to": in_reply_to,
            }
        )
        return GmailDraft(
            draft_id=draft_id,
            message_id=message_id,
            thread_id=thread_id,
        )


def _matches_any_field(m: GmailMessage, terms: List[str]) -> bool:
    haystack = " ".join(
        [
            m.subject or "",
            m.from_ or "",
            m.to or "",
            m.cc or "",
            m.body_text or "",
            m.snippet or "",
        ]
    ).lower()
    return any(t in haystack for t in terms)
