"""Gmail HTTP client (stdlib urllib only).

Implements the minimum we need from the Gmail REST API:

- list / search messages (users.messages.list, with `q` query)
- read one message (users.messages.get, format=full)
- create a draft (users.drafts.create)

Explicitly NOT implemented:

- send (no users.messages.send, no SMTP).
- OAuth browser login.
- refresh-token flow. If the credentials file carries a refresh_token,
  this client does NOT refresh; the user must keep access_token fresh.
- attachment body download.
- modify / labels / push notifications.

The credentials file format is a plain JSON object with at minimum
{"access_token": "..."}. A refresh_token / expiry / scopes may be
present but are ignored. Tokens are never echoed to stdout, stderr,
logs, repr, or error labels.

All adapter failures surface as GmailClientError with a short
non-content label and an optional cause; the body of the offending
HTTP response is not embedded in the error.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .draft import build_reply_draft_raw
from .types import (
    GmailAttachmentMeta,
    GmailDraft,
    GmailMessage,
    GmailSearchHit,
)


DEFAULT_BASE_URL = "https://gmail.googleapis.com"
DEFAULT_TIMEOUT_SEC = 30.0
LIST_PATH = "/gmail/v1/users/me/messages"
GET_PATH = "/gmail/v1/users/me/messages/{id}"
DRAFTS_PATH = "/gmail/v1/users/me/drafts"
THREAD_PATH = "/gmail/v1/users/me/threads/{id}"

_TOKEN_FIELD = "access_token"


class GmailClientError(Exception):
    """Raised for any underlying failure (network, HTTP, JSON, shape).

    The string label is intentionally short and never contains token
    material or response bodies.
    """

    def __init__(self, label: str, *, cause: Optional[BaseException] = None) -> None:
        super().__init__(label)
        self.label = label
        self.cause = cause

    def __repr__(self) -> str:
        cause_name = type(self.cause).__name__ if self.cause is not None else "None"
        return f"GmailClientError(label={self.label!r}, cause={cause_name})"


@dataclass(frozen=True)
class GmailCredentials:
    """Holds an access token loaded from a credentials JSON file.

    Construct via `GmailCredentials.from_path(...)`. The token is held
    in a private attribute; it is not exposed via repr / str.
    """

    _access_token: str

    @classmethod
    def from_path(cls, path: Path) -> "GmailCredentials":
        if not isinstance(path, Path):
            path = Path(path)
        if not path.is_file():
            raise GmailClientError(
                f"credentials file not found: {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailClientError(
                "credentials file is not valid JSON",
                cause=exc,
            ) from exc
        if not isinstance(data, dict):
            raise GmailClientError(
                "credentials file must be a JSON object"
            )
        token = data.get(_TOKEN_FIELD)
        if not isinstance(token, str) or not token.strip():
            raise GmailClientError(
                f"credentials file missing string field {_TOKEN_FIELD!r}"
            )
        return cls(_access_token=token.strip())

    def __repr__(self) -> str:
        return "GmailCredentials(<redacted>)"

    def __str__(self) -> str:
        return "GmailCredentials(<redacted>)"


def _is_https_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)


def _is_localhost_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


class GmailClient:
    """Gmail REST client using only urllib.

    Construct with a credentials path. Construction does NOT contact
    the network; the first request happens on a method call.
    """

    def __init__(
        self,
        credentials: GmailCredentials,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        allow_non_https: bool = False,
    ) -> None:
        if not isinstance(credentials, GmailCredentials):
            raise GmailClientError(
                "credentials must be a GmailCredentials instance"
            )
        if not isinstance(base_url, str) or not base_url:
            raise GmailClientError("base_url must be a non-empty string")
        normalized = base_url.rstrip("/")
        if not _is_https_url(normalized):
            if not (allow_non_https and _is_localhost_url(normalized)):
                raise GmailClientError(
                    "base_url must use https (set allow_non_https=True "
                    "only for localhost test stubs)"
                )
        if timeout_sec <= 0:
            raise GmailClientError(
                "timeout_sec must be positive"
            )
        self._credentials = credentials
        self._base_url = normalized
        self._timeout_sec = float(timeout_sec)

    def __repr__(self) -> str:
        return (
            f"GmailClient(base_url={self._base_url!r}, "
            f"timeout_sec={self._timeout_sec})"
        )

    def search(
        self,
        *,
        query: str,
        max_results: int = 10,
    ) -> Tuple[GmailSearchHit, ...]:
        if not isinstance(query, str) or not query.strip():
            raise GmailClientError("search query must be a non-empty string")
        if max_results <= 0:
            raise GmailClientError("max_results must be positive")
        params = {
            "q": query,
            "maxResults": str(int(max_results)),
        }
        url = f"{self._base_url}{LIST_PATH}?{urllib.parse.urlencode(params)}"
        decoded = self._get_json(url, label="search")
        messages = decoded.get("messages") or []
        if not isinstance(messages, list):
            raise GmailClientError("search: 'messages' field is not a list")
        hits: List[GmailSearchHit] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            tid = m.get("threadId", "")
            if not isinstance(mid, str) or not mid:
                continue
            hits.append(
                GmailSearchHit(
                    message_id=mid,
                    thread_id=tid if isinstance(tid, str) else "",
                )
            )
        return tuple(hits)

    def read(self, *, message_id: str) -> GmailMessage:
        if not isinstance(message_id, str) or not message_id.strip():
            raise GmailClientError("message_id must be a non-empty string")
        safe_id = urllib.parse.quote(message_id, safe="")
        url = (
            f"{self._base_url}"
            f"{GET_PATH.format(id=safe_id)}"
            f"?format=full"
        )
        decoded = self._get_json(url, label="read")
        return _parse_message(decoded)

    def get_thread(self, *, thread_id: str) -> Tuple[GmailMessage, ...]:
        """Fetch one thread and return its messages.

        Calls GET /gmail/v1/users/me/threads/{id}?format=full and
        parses each entry in `messages[]` through the same
        `_parse_message` used by `read()`. Bodies are extracted
        (text/plain preferred, HTML fallback) but the messages are
        intentionally returned as a tuple — the caller decides what
        to surface and whether to truncate.
        """
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise GmailClientError("thread_id must be a non-empty string")
        safe_id = urllib.parse.quote(thread_id, safe="")
        url = (
            f"{self._base_url}"
            f"{THREAD_PATH.format(id=safe_id)}"
            f"?format=full"
        )
        decoded = self._get_json(url, label="get_thread")
        msgs_raw = decoded.get("messages") or []
        if not isinstance(msgs_raw, list):
            raise GmailClientError(
                "get_thread: 'messages' field is not a list"
            )
        out: List[GmailMessage] = []
        for entry in msgs_raw:
            if not isinstance(entry, dict):
                continue
            out.append(_parse_message(entry))
        return tuple(out)

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
        raw = build_reply_draft_raw(
            to=to,
            source_subject=source_subject,
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
        payload: Dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        url = f"{self._base_url}{DRAFTS_PATH}"
        decoded = self._post_json(url, body=payload, label="create_draft")
        draft_id = decoded.get("id")
        msg = decoded.get("message") or {}
        if not isinstance(draft_id, str) or not draft_id:
            raise GmailClientError("create_draft: response missing draft id")
        message_id = msg.get("id", "") if isinstance(msg, dict) else ""
        ret_thread_id = (
            msg.get("threadId", "") if isinstance(msg, dict) else ""
        )
        return GmailDraft(
            draft_id=draft_id,
            message_id=message_id if isinstance(message_id, str) else "",
            thread_id=(
                ret_thread_id if isinstance(ret_thread_id, str) else ""
            ),
        )

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials._access_token}",
            "Accept": "application/json",
        }

    def _get_json(self, url: str, *, label: str) -> Mapping[str, Any]:
        req = urllib.request.Request(
            url, method="GET", headers=self._auth_headers()
        )
        return self._open_and_decode(req, label=label)

    def _post_json(
        self,
        url: str,
        *,
        body: Mapping[str, Any],
        label: str,
    ) -> Mapping[str, Any]:
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers=headers,
        )
        return self._open_and_decode(req, label=label)

    def _open_and_decode(
        self,
        req: urllib.request.Request,
        *,
        label: str,
    ) -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(
                req, timeout=self._timeout_sec
            ) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise GmailClientError(
                f"gmail:{label}: HTTP {exc.code}",
                cause=exc,
            ) from exc
        except urllib.error.URLError as exc:
            raise GmailClientError(
                f"gmail:{label}: network error",
                cause=exc,
            ) from exc
        except socket.timeout as exc:
            raise GmailClientError(
                f"gmail:{label}: timeout after {self._timeout_sec}s",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise GmailClientError(
                f"gmail:{label}: socket error",
                cause=exc,
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailClientError(
                f"gmail:{label}: invalid JSON response",
                cause=exc,
            ) from exc
        if not isinstance(decoded, dict):
            raise GmailClientError(
                f"gmail:{label}: response is not a JSON object"
            )
        return decoded


def _header_value(headers: Sequence[Mapping[str, Any]], name: str) -> str:
    target = name.lower()
    for h in headers:
        if not isinstance(h, dict):
            continue
        n = h.get("name")
        if isinstance(n, str) and n.lower() == target:
            v = h.get("value")
            if isinstance(v, str):
                return v
    return ""


def _walk_parts(part: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    yield part
    parts = part.get("parts")
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict):
                yield from _walk_parts(p)


def _decode_part_body(part: Mapping[str, Any]) -> str:
    body = part.get("body")
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""
    try:
        decoded = base64.urlsafe_b64decode(data.encode("ascii") + b"==")
    except (ValueError, TypeError):
        return ""
    try:
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html_lightly(html_text: str) -> str:
    # Drop <script> and <style> blocks then strip remaining tags. This is
    # a small fallback for messages that ship only text/html; full HTML
    # rendering is out of scope.
    import re

    if not html_text:
        return ""
    cleaned = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<style\b[^>]*>.*?</style>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"\s+\n", "\n", cleaned)
    return cleaned.strip()


def _parse_message(decoded: Mapping[str, Any]) -> GmailMessage:
    if not isinstance(decoded, dict):
        raise GmailClientError("read: response is not a JSON object")
    mid = decoded.get("id")
    tid = decoded.get("threadId", "")
    snippet = decoded.get("snippet", "")
    label_ids = decoded.get("labelIds") or []
    payload = decoded.get("payload") or {}
    if not isinstance(mid, str) or not mid:
        raise GmailClientError("read: response missing string 'id'")
    if not isinstance(payload, dict):
        raise GmailClientError("read: 'payload' is not an object")

    headers = payload.get("headers") or []
    if not isinstance(headers, list):
        headers = []

    subject = _header_value(headers, "Subject")
    from_ = _header_value(headers, "From")
    to_ = _header_value(headers, "To")
    cc_ = _header_value(headers, "Cc")
    date_ = _header_value(headers, "Date")
    rfc_id = _header_value(headers, "Message-ID") or _header_value(
        headers, "Message-Id"
    )

    body_text = ""
    plain_candidate = ""
    html_candidate = ""
    attachments: List[GmailAttachmentMeta] = []
    for p in _walk_parts(payload):
        mime = p.get("mimeType", "") if isinstance(p, dict) else ""
        filename = p.get("filename", "") if isinstance(p, dict) else ""
        if filename and isinstance(filename, str):
            body = p.get("body") if isinstance(p, dict) else None
            size = 0
            if isinstance(body, dict):
                s = body.get("size")
                if isinstance(s, int):
                    size = s
            attachments.append(
                GmailAttachmentMeta(
                    filename=filename,
                    mime_type=mime if isinstance(mime, str) else "",
                    size_bytes=size,
                )
            )
            continue
        if mime == "text/plain" and not plain_candidate:
            plain_candidate = _decode_part_body(p)
        elif mime == "text/html" and not html_candidate:
            html_candidate = _decode_part_body(p)

    if plain_candidate:
        body_text = plain_candidate
    elif html_candidate:
        body_text = _strip_html_lightly(html_candidate)

    return GmailMessage(
        message_id=mid,
        thread_id=tid if isinstance(tid, str) else "",
        subject=subject,
        from_=from_,
        to=to_,
        cc=cc_,
        date=date_,
        rfc822_message_id=rfc_id,
        snippet=snippet if isinstance(snippet, str) else "",
        body_text=body_text,
        has_attachments=bool(attachments),
        attachments=tuple(attachments),
        label_ids=tuple(
            x for x in label_ids if isinstance(x, str)
        ),
    )
