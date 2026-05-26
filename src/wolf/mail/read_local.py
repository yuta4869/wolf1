"""Read local mail files (.eml and .mbox) safely.

Uses Python stdlib only (email package + mailbox). No third-party
dependency. The reader extracts headers, picks the best text body
(text/plain → falls back to a naive text/html conversion), and records
whether the message has attachments. Attachments are NOT read into the
body.

`MailReadError` is raised on any safety / parse failure. The error
label deliberately contains no body bytes.
"""

from __future__ import annotations

import email
import email.message
import email.policy
import email.utils
import mailbox
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_MBOX_LIMIT = 10


class MailReadError(Exception):
    """Raised on parse / decode / size / I/O failures."""

    def __init__(self, label: str, *, path: Optional[Path] = None) -> None:
        super().__init__(label)
        self.label = label
        self.path = path

    def __repr__(self) -> str:
        p = repr(str(self.path)) if self.path is not None else "None"
        return f"MailReadError(label={self.label!r}, path={p})"


@dataclass(frozen=True)
class ParsedMail:
    subject: str
    from_: str
    to: str
    cc: str
    date: str
    message_id: str
    body: str
    has_attachments: bool
    content_type: str
    byte_size: int


class _HTMLToText(HTMLParser):
    """Minimal stdlib-only HTML-to-text converter.

    Goal is to preserve textual content for downstream LLM input; it
    does NOT preserve formatting. Block elements get newline padding;
    list items get a leading "- "; tags themselves are dropped.
    """

    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse runs of whitespace to a single space within lines,
        # collapse 3+ newlines to two newlines.
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(html)
    except Exception:
        return ""
    return parser.get_text()


def _decode_part(part: email.message.Message) -> Tuple[str, str]:
    """Return (content_type, decoded_text). Empty string on failure."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return part.get_content_type(), ""
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            return part.get_content_type(), ""
    return part.get_content_type(), text


def _extract_body(msg: email.message.Message, *, max_bytes: int) -> Tuple[str, str, bool]:
    """Pick the best textual body. Returns (body, content_type, has_attachments)."""
    if not msg.is_multipart():
        ct = msg.get_content_type()
        _, text = _decode_part(msg)
        if ct == "text/html":
            text = _html_to_text(text)
        elif ct.startswith("text/") or ct == "":
            pass
        else:
            # Single-part but non-text — treat as attachment-only.
            return ("", ct, True)
        if len(text.encode("utf-8")) > max_bytes:
            raise MailReadError(
                f"body exceeds max_bytes ({max_bytes})"
            )
        return (text, ct or "text/plain", False)

    has_attachments = False
    plain_part: Optional[email.message.Message] = None
    html_part: Optional[email.message.Message] = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ct = part.get_content_type()
        if "attachment" in disp or part.get_filename():
            has_attachments = True
            continue
        if ct == "text/plain" and plain_part is None:
            plain_part = part
        elif ct == "text/html" and html_part is None:
            html_part = part
        elif not ct.startswith("text/"):
            # inline non-text (e.g., image) — count as attachment metadata.
            has_attachments = True

    if plain_part is not None:
        _, text = _decode_part(plain_part)
        ct_used = "text/plain"
    elif html_part is not None:
        _, text = _decode_part(html_part)
        text = _html_to_text(text)
        ct_used = "text/html"
    else:
        text = ""
        ct_used = "unknown"

    if len(text.encode("utf-8")) > max_bytes:
        raise MailReadError(
            f"body exceeds max_bytes ({max_bytes})"
        )
    return (text, ct_used, has_attachments)


def _parsed_from_message(
    msg: email.message.Message,
    *,
    max_bytes: int,
) -> ParsedMail:
    subject = msg.get("Subject", "") or ""
    from_ = msg.get("From", "") or ""
    to = msg.get("To", "") or ""
    cc = msg.get("Cc", "") or ""
    date = msg.get("Date", "") or ""
    message_id = msg.get("Message-ID", "") or msg.get("Message-Id", "") or ""
    body, content_type, has_attachments = _extract_body(msg, max_bytes=max_bytes)
    return ParsedMail(
        subject=subject.strip(),
        from_=from_.strip(),
        to=to.strip(),
        cc=cc.strip(),
        date=date.strip(),
        message_id=message_id.strip(),
        body=body,
        has_attachments=has_attachments,
        content_type=content_type,
        byte_size=len(body.encode("utf-8")),
    )


def read_eml(path: Path, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> ParsedMail:
    if path is None:
        raise MailReadError("path is None")
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists():
        raise MailReadError("file not found", path=path)
    if not path.is_file():
        raise MailReadError("not a regular file", path=path)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise MailReadError(
            f"read failed ({type(exc).__name__})", path=path
        ) from exc
    if len(blob) > max_bytes * 4:
        # Allow .eml total size up to 4x body limit (headers + encoding overhead).
        raise MailReadError(
            f"eml total size exceeds {max_bytes * 4}", path=path
        )
    try:
        msg = email.message_from_bytes(blob, policy=email.policy.default)
    except Exception as exc:
        raise MailReadError(
            f"parse failed ({type(exc).__name__})", path=path
        ) from exc
    return _parsed_from_message(msg, max_bytes=max_bytes)


@dataclass(frozen=True)
class MboxReadResult:
    messages: Tuple[ParsedMail, ...]
    skipped: Tuple[str, ...] = field(default_factory=tuple)


def read_mbox(
    path: Path,
    *,
    limit: int = DEFAULT_MBOX_LIMIT,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    filter_subject: Optional[str] = None,
    filter_from: Optional[str] = None,
    filter_body_contains: Optional[str] = None,
) -> MboxReadResult:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists():
        raise MailReadError("file not found", path=path)
    if not path.is_file():
        raise MailReadError("not a regular file", path=path)
    try:
        mb = mailbox.mbox(str(path))
    except Exception as exc:
        raise MailReadError(
            f"mbox open failed ({type(exc).__name__})", path=path
        ) from exc

    parsed: List[ParsedMail] = []
    skipped: List[str] = []
    try:
        for key, raw_msg in mb.items():
            if len(parsed) >= limit:
                break
            try:
                pm = _parsed_from_message(raw_msg, max_bytes=max_bytes)
            except MailReadError as exc:
                skipped.append(f"message {key}: {exc.label}")
                continue
            if filter_subject and filter_subject.lower() not in pm.subject.lower():
                continue
            if filter_from and filter_from.lower() not in pm.from_.lower():
                continue
            if (
                filter_body_contains
                and filter_body_contains.lower() not in pm.body.lower()
            ):
                continue
            parsed.append(pm)
    finally:
        mb.close()

    return MboxReadResult(messages=tuple(parsed), skipped=tuple(skipped))


def read_mail_any(
    path: Path,
    *,
    limit: int = DEFAULT_MBOX_LIMIT,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> MboxReadResult:
    """Convenience: auto-detect .eml vs .mbox by extension.

    Returns a MboxReadResult in both cases; for a single .eml the
    `messages` tuple has length 1.
    """
    if not isinstance(path, Path):
        path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".mbox":
        return read_mbox(path, limit=limit, max_bytes=max_bytes)
    if suffix == ".eml" or suffix == "":
        pm = read_eml(path, max_bytes=max_bytes)
        return MboxReadResult(messages=(pm,))
    # Unknown extension — try .eml semantics.
    pm = read_eml(path, max_bytes=max_bytes)
    return MboxReadResult(messages=(pm,))
