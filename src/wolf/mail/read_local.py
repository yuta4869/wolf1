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
from typing import List, Optional, Sequence, Tuple, Union


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
class AttachmentMeta:
    """Metadata for a single attachment. Payload bytes are never stored."""

    filename: str  # empty string if the part has no filename
    content_type: str
    size_bytes: int  # size of the encoded payload as it appeared in the mail


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
    attachments: Tuple[AttachmentMeta, ...] = field(default_factory=tuple)


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


def _attachment_meta_for_part(
    part: email.message.Message,
) -> AttachmentMeta:
    """Build AttachmentMeta WITHOUT decoding the payload into memory.

    We use part.get_payload(decode=False) and then len() — for base64
    parts this is the encoded payload length (slightly larger than the
    decoded size, but accurate as a "how big is this attachment as it
    appeared in the mail" indicator). Avoiding decode keeps the
    attachment bytes out of memory and out of error messages.
    """
    filename = part.get_filename() or ""
    ct = part.get_content_type() or ""
    raw = part.get_payload(decode=False)
    if isinstance(raw, str):
        size_bytes = len(raw.encode("utf-8", errors="replace"))
    elif isinstance(raw, (bytes, bytearray)):
        size_bytes = len(raw)
    elif isinstance(raw, list):
        # Nested multipart; report 0 here and the outer walk will
        # surface the inner parts.
        size_bytes = 0
    else:
        size_bytes = 0
    return AttachmentMeta(
        filename=filename,
        content_type=ct,
        size_bytes=size_bytes,
    )


def _extract_body(
    msg: email.message.Message, *, max_bytes: int
) -> Tuple[str, str, bool, Tuple[AttachmentMeta, ...]]:
    """Pick the best textual body.

    Returns (body, content_type, has_attachments, attachments).
    """
    if not msg.is_multipart():
        ct = msg.get_content_type()
        _, text = _decode_part(msg)
        if ct == "text/html":
            text = _html_to_text(text)
        elif ct.startswith("text/") or ct == "":
            pass
        else:
            # Single-part but non-text — treat as attachment-only.
            meta = _attachment_meta_for_part(msg)
            return ("", ct, True, (meta,))
        if len(text.encode("utf-8")) > max_bytes:
            raise MailReadError(
                f"body exceeds max_bytes ({max_bytes})"
            )
        return (text, ct or "text/plain", False, ())

    has_attachments = False
    plain_part: Optional[email.message.Message] = None
    html_part: Optional[email.message.Message] = None
    attachments: List[AttachmentMeta] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ct = part.get_content_type()
        if "attachment" in disp or part.get_filename():
            has_attachments = True
            attachments.append(_attachment_meta_for_part(part))
            continue
        if ct == "text/plain" and plain_part is None:
            plain_part = part
        elif ct == "text/html" and html_part is None:
            html_part = part
        elif not ct.startswith("text/"):
            # inline non-text (e.g., image) — record as attachment.
            has_attachments = True
            attachments.append(_attachment_meta_for_part(part))

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
    return (text, ct_used, has_attachments, tuple(attachments))


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
    body, content_type, has_attachments, attachments = _extract_body(
        msg, max_bytes=max_bytes
    )
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
        attachments=attachments,
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


FilterArg = Union[None, str, Sequence[str]]


def _filter_matches(haystack: str, needle: FilterArg) -> bool:
    """Return True if `needle` is absent (no filter) OR any provided
    substring is found in `haystack`.

    - None / "" / empty list -> no filter, returns True.
    - str -> single-substring AND-style; True iff haystack contains it.
    - Sequence[str] -> OR semantics; True iff any element matches.
    """
    if needle is None:
        return True
    if isinstance(needle, str):
        if not needle:
            return True
        return needle.lower() in haystack.lower()
    # Sequence[str]
    items = [n for n in needle if n]
    if not items:
        return True
    h_lower = haystack.lower()
    return any(n.lower() in h_lower for n in items)


def _pass_three_filters(
    pm: ParsedMail,
    *,
    filter_subject: FilterArg,
    filter_from: FilterArg,
    filter_body_contains: FilterArg,
) -> bool:
    if not _filter_matches(pm.subject, filter_subject):
        return False
    if not _filter_matches(pm.from_, filter_from):
        return False
    if not _filter_matches(pm.body, filter_body_contains):
        return False
    return True


def read_mbox(
    path: Path,
    *,
    limit: int = DEFAULT_MBOX_LIMIT,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    filter_subject: FilterArg = None,
    filter_from: FilterArg = None,
    filter_body_contains: FilterArg = None,
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
            if not _pass_three_filters(
                pm,
                filter_subject=filter_subject,
                filter_from=filter_from,
                filter_body_contains=filter_body_contains,
            ):
                continue
            parsed.append(pm)
    finally:
        mb.close()

    return MboxReadResult(messages=tuple(parsed), skipped=tuple(skipped))


def _eml_passes_filters(
    pm: ParsedMail,
    *,
    filter_subject: FilterArg,
    filter_from: FilterArg,
    filter_body_contains: FilterArg,
) -> bool:
    return _pass_three_filters(
        pm,
        filter_subject=filter_subject,
        filter_from=filter_from,
        filter_body_contains=filter_body_contains,
    )


def _is_maildir(path: Path) -> bool:
    """Maildir-style directory: contains cur/, new/, tmp/ subdirectories."""
    if not path.is_dir():
        return False
    return all(
        (path / sub).is_dir() for sub in ("cur", "new", "tmp")
    )


def read_maildir(
    path: Path,
    *,
    limit: int = DEFAULT_MBOX_LIMIT,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    filter_subject: FilterArg = None,
    filter_from: FilterArg = None,
    filter_body_contains: FilterArg = None,
) -> MboxReadResult:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists():
        raise MailReadError("file not found", path=path)
    if not _is_maildir(path):
        raise MailReadError(
            "not a Maildir (missing cur/ new/ tmp/)", path=path
        )
    try:
        md = mailbox.Maildir(str(path), create=False)
    except Exception as exc:
        raise MailReadError(
            f"maildir open failed ({type(exc).__name__})", path=path
        ) from exc

    parsed: List[ParsedMail] = []
    skipped: List[str] = []
    try:
        for key, raw_msg in md.items():
            if len(parsed) >= limit:
                break
            try:
                pm = _parsed_from_message(raw_msg, max_bytes=max_bytes)
            except MailReadError as exc:
                skipped.append(f"message {key}: {exc.label}")
                continue
            if not _pass_three_filters(
                pm,
                filter_subject=filter_subject,
                filter_from=filter_from,
                filter_body_contains=filter_body_contains,
            ):
                continue
            parsed.append(pm)
    finally:
        md.close()

    # Sort by date string for deterministic ordering; mailbox.Maildir
    # iteration order depends on filesystem readdir which is not stable
    # across filesystems / OSes.
    parsed.sort(key=lambda p: (p.date, p.message_id))
    return MboxReadResult(messages=tuple(parsed), skipped=tuple(skipped))


def read_mail_any(
    path: Path,
    *,
    limit: int = DEFAULT_MBOX_LIMIT,
    max_bytes: int = DEFAULT_MAX_BODY_BYTES,
    filter_subject: FilterArg = None,
    filter_from: FilterArg = None,
    filter_body_contains: FilterArg = None,
) -> MboxReadResult:
    """Convenience: auto-detect .eml / .mbox / Maildir directory.

    Returns a MboxReadResult in all cases; for a single .eml the
    `messages` tuple has length 1 (or 0 if filters drop the message).
    """
    if not isinstance(path, Path):
        path = Path(path)
    # Directory case: try Maildir first.
    if path.is_dir():
        if _is_maildir(path):
            return read_maildir(
                path,
                limit=limit,
                max_bytes=max_bytes,
                filter_subject=filter_subject,
                filter_from=filter_from,
                filter_body_contains=filter_body_contains,
            )
        raise MailReadError(
            "directory is not a Maildir (missing cur/ new/ tmp/)",
            path=path,
        )
    suffix = path.suffix.lower()
    if suffix == ".mbox":
        return read_mbox(
            path,
            limit=limit,
            max_bytes=max_bytes,
            filter_subject=filter_subject,
            filter_from=filter_from,
            filter_body_contains=filter_body_contains,
        )
    if suffix == ".eml" or suffix == "":
        pm = read_eml(path, max_bytes=max_bytes)
        if not _eml_passes_filters(
            pm,
            filter_subject=filter_subject,
            filter_from=filter_from,
            filter_body_contains=filter_body_contains,
        ):
            return MboxReadResult(messages=())
        return MboxReadResult(messages=(pm,))
    # Unknown extension — try .eml semantics.
    pm = read_eml(path, max_bytes=max_bytes)
    if not _eml_passes_filters(
        pm,
        filter_subject=filter_subject,
        filter_from=filter_from,
        filter_body_contains=filter_body_contains,
    ):
        return MboxReadResult(messages=())
    return MboxReadResult(messages=(pm,))


