"""RFC 2822 + base64url helpers for Gmail draft creation.

Gmail's drafts.create endpoint expects:

  {"message": {"raw": "<base64url(RFC2822)>"}}

This module builds that raw payload without pulling in any third-party
library. The mail body is treated as opaque text; the caller (CLI) is
responsible for any prompt-injection / safety pipeline.
"""

from __future__ import annotations

import base64
import email.message
import email.policy


def _normalize_subject(source_subject: str) -> str:
    """Build a reply-style subject ('Re: <source>') without doubling 'Re:'."""
    s = (source_subject or "").strip()
    if not s:
        return "Re:"
    lowered = s.lower()
    if lowered.startswith("re:"):
        return s
    return f"Re: {s}"


def build_reply_draft_raw(
    *,
    to: str,
    source_subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    """Build a base64url RFC2822 reply payload for Gmail drafts.create.

    Args:
        to: recipient (typically the source message's From header).
        source_subject: subject of the message being replied to; the
            output subject is "Re: <source>" unless source already
            starts with "Re:".
        body: the reply text. Treated as opaque; the caller is
            responsible for any safety / injection scanning.
        in_reply_to: optional source RFC2822 Message-ID for threading.
        references: optional References header value.

    Returns:
        The base64url-encoded RFC2822 message, suitable for the
        Gmail API's message.raw field.
    """
    msg = email.message.EmailMessage(policy=email.policy.SMTP)
    msg["To"] = to or ""
    msg["Subject"] = _normalize_subject(source_subject)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body or "", subtype="plain", charset="utf-8")
    raw_bytes = msg.as_bytes()
    encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
    return encoded
