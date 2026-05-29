"""Tiny iCalendar (.ics) draft writer.

We emit one VCALENDAR with one VEVENT per
`CalendarEventCandidate`. The output is intentionally minimal: no
RRULE, no VALARM, no organizer. Use cases:

- write a .ics file the user can import into their calendar app.
- pipe to stdout for inspection.

Times without an explicit timezone are emitted as UTC
(DTSTART:YYYYMMDDTHHMMSSZ). Dates without a time are emitted as
all-day events (DTSTART;VALUE=DATE:YYYYMMDD).

UIDs are deterministic: SHA-256 of
(source_id, title, start_date, start_time) truncated to 16 hex.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Iterable, List

from .types import CalendarEventCandidate


PRODID = "-//wolf//v0.3 task pack//EN"
VERSION = "2.0"


_DATE_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")


def _escape(text: str) -> str:
    """Escape iCalendar TEXT field per RFC 5545."""
    if not text:
        return ""
    out = text.replace("\\", "\\\\")
    out = out.replace(";", r"\;")
    out = out.replace(",", r"\,")
    out = out.replace("\n", r"\n")
    out = out.replace("\r", "")
    return out


def _fold(line: str) -> str:
    """Fold long content lines per RFC 5545 (75-octet limit)."""
    if len(line) <= 75:
        return line
    out_chunks: List[str] = [line[:75]]
    rest = line[75:]
    while rest:
        # Subsequent lines start with a single space.
        out_chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(out_chunks)


def _stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _to_date_compact(date_str: str) -> str:
    m = _DATE_RE.match(date_str)
    if not m:
        return ""
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"


def _to_time_compact(time_str: str) -> str:
    m = _TIME_RE.match(time_str)
    if not m:
        return ""
    hh, mm, ss = m.group(1), m.group(2), m.group(3) or "00"
    return f"{int(hh):02d}{mm}{ss}"


def _stable_uid(event: CalendarEventCandidate) -> str:
    seed = "|".join(
        [
            event.source_id or "",
            event.title or "",
            event.start_date or "",
            event.start_time or "",
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{digest}@wolf.local"


def _build_event_block(event: CalendarEventCandidate) -> List[str]:
    """Return one VEVENT's lines (not yet folded)."""
    start_date = _to_date_compact(event.start_date)
    if not start_date:
        return []
    end_date = _to_date_compact(event.end_date) or start_date
    start_time = _to_time_compact(event.start_time)
    end_time = _to_time_compact(event.end_time)
    uid = _stable_uid(event)
    description = _escape(
        (
            f"From: {event.source_subject}"
            if event.source_subject
            else ""
        )
        + (
            (("\n" if event.source_subject else "") + event.description)
            if event.description
            else ""
        )
        + (
            (
                ("\n" if (event.source_subject or event.description) else "")
                + f"Evidence: {event.evidence_snippet}"
            )
            if event.evidence_snippet
            else ""
        )
    )
    lines: List[str] = ["BEGIN:VEVENT"]
    lines.append(f"UID:{uid}")
    lines.append(f"DTSTAMP:{_stamp_now()}")
    if start_time:
        # Timed event. We coerce to UTC when no timezone is specified
        # or when the timezone is "UTC".
        if event.timezone in ("", "UTC"):
            lines.append(f"DTSTART:{start_date}T{start_time}Z")
            lines.append(
                f"DTEND:{end_date}T"
                f"{end_time or start_time}Z"
            )
        else:
            # Non-UTC: emit floating local time with TZID hint.
            tzid = _escape(event.timezone)
            lines.append(f"DTSTART;TZID={tzid}:{start_date}T{start_time}")
            lines.append(
                f"DTEND;TZID={tzid}:{end_date}T"
                f"{end_time or start_time}"
            )
    else:
        # All-day event.
        lines.append(f"DTSTART;VALUE=DATE:{start_date}")
        lines.append(f"DTEND;VALUE=DATE:{end_date}")
    lines.append(f"SUMMARY:{_escape(event.title)}")
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    for a in event.attendees:
        lines.append(f"ATTENDEE:{_escape(a)}")
    lines.append("END:VEVENT")
    return lines


def build_ics(events: Iterable[CalendarEventCandidate]) -> str:
    """Build a single VCALENDAR string from candidates.

    Events with missing / malformed `start_date` are skipped
    silently. Use `[c for c in candidates if c.start_date]` ahead
    of time if you want to surface skips.
    """
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        f"VERSION:{VERSION}",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for ev in events:
        block = _build_event_block(ev)
        if block:
            lines.extend(block)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
