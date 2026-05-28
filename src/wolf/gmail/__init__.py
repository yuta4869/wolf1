"""Gmail read / draft adapter (read-only API + drafts; no send).

This package is intentionally narrow:
- search and read messages (list / get with format=full).
- create drafts (POST /drafts).
- it does NOT implement send, SMTP, IMAP, OAuth login, token refresh,
  attachment download, push notifications, or any background sync.

The real client speaks Gmail's JSON API over urllib (stdlib only); no
google-api-python-client dependency. Tokens are read from an explicit
credentials file path; refresh tokens, if present, are NOT refreshed
by this module. The default backend for the CLI and tests is the
in-memory FakeGmailClient.
"""

from .client import (
    DEFAULT_BASE_URL,
    GmailClient,
    GmailClientError,
    GmailCredentials,
)
from .draft import build_reply_draft_raw
from .fake import FakeGmailClient
from .types import GmailDraft, GmailMessage, GmailSearchHit

__all__ = [
    "DEFAULT_BASE_URL",
    "FakeGmailClient",
    "GmailClient",
    "GmailClientError",
    "GmailCredentials",
    "GmailDraft",
    "GmailMessage",
    "GmailSearchHit",
    "build_reply_draft_raw",
]
