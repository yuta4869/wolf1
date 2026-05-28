"""Gmail data types shared between real and fake clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class GmailSearchHit:
    """A single search-result row.

    Mirrors what Gmail's users.messages.list returns: an id and a
    threadId. Headers / snippet / body require a separate read call.
    """

    message_id: str
    thread_id: str


@dataclass(frozen=True)
class GmailAttachmentMeta:
    """Attachment metadata without the body bytes.

    The body is intentionally not fetched. The client returns name,
    MIME type, and size when available.
    """

    filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class GmailMessage:
    """A single message after format=full extraction.

    `body_text` carries the text/plain part if present, else a simple
    HTML-to-text fallback. Attachment payloads are not loaded.
    """

    message_id: str
    thread_id: str
    subject: str
    from_: str
    to: str
    cc: str
    date: str
    rfc822_message_id: str
    snippet: str
    body_text: str
    has_attachments: bool
    attachments: Tuple[GmailAttachmentMeta, ...] = field(default_factory=tuple)
    label_ids: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GmailDraft:
    """Result of a draft creation call."""

    draft_id: str
    message_id: str
    thread_id: str
