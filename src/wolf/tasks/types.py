"""Dataclasses for task and calendar-event candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class TaskCandidate:
    """A task extracted from a single mail.

    The raw mail body is NOT carried here. `evidence_snippet` is a
    bounded slice of the body that supports the extraction; it is
    intentionally short.
    """

    title: str
    description: str
    due_date: str  # "YYYY-MM-DD" or ""
    due_time: str  # "HH:MM:SS" or ""
    timezone: str  # e.g. "UTC", "+09:00" or ""
    source_kind: str  # "local_mail" | "gmail"
    source_id: str
    source_subject: str
    source_from: str
    confidence: float  # 0.0..1.0
    evidence_snippet: str  # bounded; never the full body

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "due_date": self.due_date,
            "due_time": self.due_time,
            "timezone": self.timezone,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_subject": self.source_subject,
            "source_from": self.source_from,
            "confidence": self.confidence,
            "evidence_snippet": self.evidence_snippet,
        }


@dataclass(frozen=True)
class CalendarEventCandidate:
    """A calendar event extracted from a single mail."""

    title: str
    description: str
    start_date: str  # "YYYY-MM-DD"
    start_time: str  # "HH:MM:SS" or "" for all-day
    end_date: str
    end_time: str
    timezone: str
    location: str
    attendees: Tuple[str, ...] = field(default_factory=tuple)
    source_kind: str = ""
    source_id: str = ""
    source_subject: str = ""
    confidence: float = 0.0
    evidence_snippet: str = ""

    def is_all_day(self) -> bool:
        return not self.start_time

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "start_date": self.start_date,
            "start_time": self.start_time,
            "end_date": self.end_date,
            "end_time": self.end_time,
            "timezone": self.timezone,
            "location": self.location,
            "attendees": list(self.attendees),
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_subject": self.source_subject,
            "confidence": self.confidence,
            "evidence_snippet": self.evidence_snippet,
        }
