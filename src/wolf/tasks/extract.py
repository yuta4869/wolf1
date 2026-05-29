"""Task / calendar candidate extraction.

Pipeline (LLM-first, heuristic-fallback):

1. The caller passes the mail body (already validated by the
   Router-side `UntrustedText` wrap).
2. We send a structured "extract JSON" instruction to the LLM via
   the Router. The expected response is one JSON object with
   `tasks` and `events` arrays.
3. If parsing fails, we fall back to a deterministic regex
   heuristic that picks up "Action item:", "TODO:", "Due:",
   "Meeting:" / "Mtg:", and a few ISO-style dates from the body.

The fallback exists so that the in-process FakeLLM (which returns
"SUMMARY(...)" boilerplate, not JSON) can still drive the CLI in
tests and `network_mode: none` Docker runs without raising. The
heuristic does NOT cover every mail form; it is "good enough for
the fake path", not a substitute for a real model on real mail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .types import CalendarEventCandidate, TaskCandidate


EXTRACTION_INSTRUCTION = (
    "Extract any actionable tasks and calendar events from the mail "
    "body below. Reply with a single JSON object of the form "
    '{"tasks": [...], "events": [...]} where each task carries '
    "title, due_date (YYYY-MM-DD or empty), due_time "
    "(HH:MM:SS or empty), and timezone; and each event carries "
    "title, start_date, start_time, end_date, end_time, timezone, "
    "location, and attendees. Use empty strings when a field is "
    "unknown. Do not invent details."
)


EVIDENCE_MAX_BYTES = 240


def _truncate_evidence(text: str, *, max_bytes: int = EVIDENCE_MAX_BYTES) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    for back in range(0, 4):
        try:
            return cut[: len(cut) - back].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return cut.decode("utf-8", errors="replace")


@dataclass
class ExtractionResult:
    tasks: List[TaskCandidate] = field(default_factory=list)
    events: List[CalendarEventCandidate] = field(default_factory=list)
    used_fallback: bool = False
    warnings: List[str] = field(default_factory=list)


_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_ISO_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b")
_ACTION_RE = re.compile(
    r"^[ \t-]*(?:action(?:\s*item)?|todo|task)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_DUE_RE = re.compile(
    r"^[ \t-]*(?:due|deadline|期限)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_MEETING_RE = re.compile(
    r"^[ \t-]*(?:meeting|mtg|event|予定|打ち合わせ)\s*[:：]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def _first_iso_date(text: str) -> str:
    m = _ISO_DATE_RE.search(text or "")
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def _first_iso_time(text: str) -> str:
    m = _ISO_TIME_RE.search(text or "")
    if not m:
        return ""
    hh, mm, ss = m.group(1), m.group(2), m.group(3) or "00"
    return f"{int(hh):02d}:{mm}:{ss}"


def _heuristic_tasks(
    body: str,
    *,
    source_kind: str,
    source_id: str,
    source_subject: str,
    source_from: str,
) -> List[TaskCandidate]:
    out: List[TaskCandidate] = []
    seen_titles: set = set()
    for match in _ACTION_RE.finditer(body):
        title = match.group(1).strip().rstrip(".")
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        out.append(
            TaskCandidate(
                title=title,
                description="",
                due_date=_first_iso_date(title) or _first_iso_date(body),
                due_time="",
                timezone="UTC",
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
                source_from=source_from,
                confidence=0.5,
                evidence_snippet=_truncate_evidence(match.group(0).strip()),
            )
        )
    for match in _DUE_RE.finditer(body):
        raw = match.group(1).strip().rstrip(".")
        due = _first_iso_date(raw)
        if not due:
            continue
        title = (source_subject or raw)[:80]
        if title in seen_titles:
            continue
        seen_titles.add(title)
        out.append(
            TaskCandidate(
                title=title,
                description=raw,
                due_date=due,
                due_time="",
                timezone="UTC",
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
                source_from=source_from,
                confidence=0.55,
                evidence_snippet=_truncate_evidence(match.group(0).strip()),
            )
        )
    return out


def _heuristic_events(
    body: str,
    *,
    source_kind: str,
    source_id: str,
    source_subject: str,
) -> List[CalendarEventCandidate]:
    out: List[CalendarEventCandidate] = []
    for match in _MEETING_RE.finditer(body):
        line = match.group(1).strip().rstrip(".")
        date = _first_iso_date(line) or _first_iso_date(body)
        if not date:
            # Without a date the event candidate is not actionable; skip.
            continue
        time_ = _first_iso_time(line) or _first_iso_time(body)
        title = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", "", line).strip(" ,:-") or (
            source_subject or "Meeting"
        )
        out.append(
            CalendarEventCandidate(
                title=title[:120],
                description="",
                start_date=date,
                start_time=time_,
                end_date=date,
                end_time="",
                timezone="UTC",
                location="",
                attendees=(),
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
                confidence=0.5,
                evidence_snippet=_truncate_evidence(match.group(0).strip()),
            )
        )
    return out


def _coerce_task(
    raw: Mapping[str, Any],
    *,
    source_kind: str,
    source_id: str,
    source_subject: str,
    source_from: str,
) -> Optional[TaskCandidate]:
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    description = (raw.get("description") or "").strip()
    due_date = (raw.get("due_date") or "").strip()
    due_time = (raw.get("due_time") or "").strip()
    timezone = (raw.get("timezone") or "").strip() or "UTC"
    confidence_raw = raw.get("confidence", 0.6)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    evidence = _truncate_evidence((raw.get("evidence_snippet") or "").strip())
    return TaskCandidate(
        title=title,
        description=description,
        due_date=due_date,
        due_time=due_time,
        timezone=timezone,
        source_kind=source_kind,
        source_id=source_id,
        source_subject=source_subject,
        source_from=source_from,
        confidence=confidence,
        evidence_snippet=evidence,
    )


def _coerce_event(
    raw: Mapping[str, Any],
    *,
    source_kind: str,
    source_id: str,
    source_subject: str,
) -> Optional[CalendarEventCandidate]:
    title = (raw.get("title") or "").strip()
    start_date = (raw.get("start_date") or "").strip()
    if not title or not start_date:
        return None
    description = (raw.get("description") or "").strip()
    start_time = (raw.get("start_time") or "").strip()
    end_date = (raw.get("end_date") or "").strip() or start_date
    end_time = (raw.get("end_time") or "").strip()
    timezone = (raw.get("timezone") or "").strip() or "UTC"
    location = (raw.get("location") or "").strip()
    attendees_raw = raw.get("attendees") or []
    if isinstance(attendees_raw, list):
        attendees = tuple(
            str(a).strip() for a in attendees_raw if str(a).strip()
        )
    else:
        attendees = ()
    confidence_raw = raw.get("confidence", 0.6)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    evidence = _truncate_evidence((raw.get("evidence_snippet") or "").strip())
    return CalendarEventCandidate(
        title=title,
        description=description,
        start_date=start_date,
        start_time=start_time,
        end_date=end_date,
        end_time=end_time,
        timezone=timezone,
        location=location,
        attendees=attendees,
        source_kind=source_kind,
        source_id=source_id,
        source_subject=source_subject,
        confidence=confidence,
        evidence_snippet=evidence,
    )


def _try_parse_json(s: str) -> Optional[Mapping[str, Any]]:
    if not s:
        return None
    s = s.strip()
    # Strip simple markdown fences like ```json ... ```.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_]*\n?", "", s, count=1)
        s = re.sub(r"\n?```\s*$", "", s, count=1)
    try:
        decoded = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def extract_candidates_from_text(
    *,
    llm_output: str,
    body: str,
    source_kind: str,
    source_id: str,
    source_subject: str,
    source_from: str,
) -> ExtractionResult:
    """Parse LLM output as JSON; fall back to heuristic over body."""
    result = ExtractionResult()
    decoded = _try_parse_json(llm_output)
    if decoded is not None:
        for raw_t in decoded.get("tasks") or []:
            if not isinstance(raw_t, dict):
                continue
            t = _coerce_task(
                raw_t,
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
                source_from=source_from,
            )
            if t is not None:
                result.tasks.append(t)
        for raw_e in decoded.get("events") or []:
            if not isinstance(raw_e, dict):
                continue
            e = _coerce_event(
                raw_e,
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
            )
            if e is not None:
                result.events.append(e)
        if not result.tasks and not result.events:
            # JSON parsed but empty / unusable → fall back to heuristic.
            result.used_fallback = True
            result.tasks.extend(
                _heuristic_tasks(
                    body,
                    source_kind=source_kind,
                    source_id=source_id,
                    source_subject=source_subject,
                    source_from=source_from,
                )
            )
            result.events.extend(
                _heuristic_events(
                    body,
                    source_kind=source_kind,
                    source_id=source_id,
                    source_subject=source_subject,
                )
            )
        return result

    result.used_fallback = True
    result.warnings.append("llm_output_not_json")
    result.tasks.extend(
        _heuristic_tasks(
            body,
            source_kind=source_kind,
            source_id=source_id,
            source_subject=source_subject,
            source_from=source_from,
        )
    )
    result.events.extend(
        _heuristic_events(
            body,
            source_kind=source_kind,
            source_id=source_id,
            source_subject=source_subject,
        )
    )
    return result


def extract_candidates(
    *,
    llm,
    body: str,
    source_kind: str,
    source_id: str,
    source_subject: str,
    source_from: str = "",
    max_tokens: int = 512,
) -> ExtractionResult:
    """Drive the LLM and convert its response into candidates.

    The caller is responsible for any Router / prompt-injection
    handling around the LLM. This function only calls
    `llm.generate(prompt, max_tokens=...)` and then parses.
    """
    if not body or not body.strip():
        return ExtractionResult()
    prompt = (
        f"{EXTRACTION_INSTRUCTION}\n\n"
        "<MAIL_BODY>\n"
        f"{body}\n"
        "</MAIL_BODY>\n"
    )
    try:
        raw_output = llm.generate(prompt, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        result = ExtractionResult()
        result.warnings.append(f"llm_error: {type(exc).__name__}")
        # Still try heuristic so the CLI is useful when LLM fails.
        result.tasks.extend(
            _heuristic_tasks(
                body,
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
                source_from=source_from,
            )
        )
        result.events.extend(
            _heuristic_events(
                body,
                source_kind=source_kind,
                source_id=source_id,
                source_subject=source_subject,
            )
        )
        result.used_fallback = True
        return result
    if not isinstance(raw_output, str):
        raw_output = str(raw_output) if raw_output is not None else ""
    return extract_candidates_from_text(
        llm_output=raw_output,
        body=body,
        source_kind=source_kind,
        source_id=source_id,
        source_subject=source_subject,
        source_from=source_from,
    )
