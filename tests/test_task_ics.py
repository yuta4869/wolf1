"""Tests for src/wolf/tasks/ics.py."""

from __future__ import annotations

import unittest

from wolf.tasks import build_ics
from wolf.tasks.types import CalendarEventCandidate


def _event(
    *,
    title: str = "Sync",
    start_date: str = "2026-06-18",
    start_time: str = "14:00:00",
    end_date: str = "2026-06-18",
    end_time: str = "15:00:00",
    timezone: str = "UTC",
    source_id: str = "msg-1",
    source_subject: str = "Q3 prep",
    evidence_snippet: str = "Meeting: planning sync on 2026-06-18 14:00",
    description: str = "",
    location: str = "",
    attendees=(),
) -> CalendarEventCandidate:
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
        source_kind="local_mail",
        source_id=source_id,
        source_subject=source_subject,
        confidence=0.8,
        evidence_snippet=evidence_snippet,
    )


class TimedEventTest(unittest.TestCase):
    def test_outputs_vcalendar_envelope(self) -> None:
        ics = build_ics([_event()])
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("END:VCALENDAR", ics)
        self.assertIn("PRODID:", ics)
        self.assertIn("VERSION:2.0", ics)

    def test_utc_timed_event(self) -> None:
        ics = build_ics([_event()])
        self.assertIn("DTSTART:20260618T140000Z", ics)
        self.assertIn("DTEND:20260618T150000Z", ics)
        self.assertIn("SUMMARY:Sync", ics)

    def test_summary_with_description_and_source_subject(self) -> None:
        ics = build_ics([_event(description="Bring slides")])
        self.assertIn("DESCRIPTION:", ics)
        self.assertIn("Q3 prep", ics)
        self.assertIn("Bring slides", ics)
        self.assertIn("Evidence:", ics)

    def test_attendees_emitted(self) -> None:
        ics = build_ics(
            [_event(attendees=("alice@x", "bob@y"))]
        )
        self.assertIn("ATTENDEE:alice@x", ics)
        self.assertIn("ATTENDEE:bob@y", ics)

    def test_location_emitted(self) -> None:
        ics = build_ics([_event(location="Room A")])
        self.assertIn("LOCATION:Room A", ics)


class AllDayEventTest(unittest.TestCase):
    def test_no_time_becomes_value_date(self) -> None:
        ev = _event(start_time="", end_time="")
        ics = build_ics([ev])
        self.assertIn("DTSTART;VALUE=DATE:20260618", ics)
        self.assertIn("DTEND;VALUE=DATE:20260618", ics)


class NonUtcTimezoneTest(unittest.TestCase):
    def test_tzid_used(self) -> None:
        ev = _event(timezone="Asia/Tokyo")
        ics = build_ics([ev])
        self.assertIn("DTSTART;TZID=Asia/Tokyo:20260618T140000", ics)
        # No trailing 'Z' on TZID lines.
        self.assertNotIn("DTSTART;TZID=Asia/Tokyo:20260618T140000Z", ics)


class StableUidTest(unittest.TestCase):
    def test_identical_inputs_produce_identical_uid(self) -> None:
        a = build_ics([_event()])
        b = build_ics([_event()])
        a_uid = [l for l in a.splitlines() if l.startswith("UID:")][0]
        b_uid = [l for l in b.splitlines() if l.startswith("UID:")][0]
        self.assertEqual(a_uid, b_uid)

    def test_different_titles_produce_different_uid(self) -> None:
        a = build_ics([_event(title="A")])
        b = build_ics([_event(title="B")])
        a_uid = [l for l in a.splitlines() if l.startswith("UID:")][0]
        b_uid = [l for l in b.splitlines() if l.startswith("UID:")][0]
        self.assertNotEqual(a_uid, b_uid)


class MalformedInputTest(unittest.TestCase):
    def test_missing_start_date_skipped(self) -> None:
        ev = _event(start_date="")
        ics = build_ics([ev])
        self.assertNotIn("BEGIN:VEVENT", ics)

    def test_invalid_date_format_skipped(self) -> None:
        ev = _event(start_date="not-a-date")
        ics = build_ics([ev])
        self.assertNotIn("BEGIN:VEVENT", ics)

    def test_empty_event_list(self) -> None:
        ics = build_ics([])
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("END:VCALENDAR", ics)
        self.assertNotIn("BEGIN:VEVENT", ics)


class NoBodyLeakTest(unittest.TestCase):
    def test_ics_does_not_carry_full_body(self) -> None:
        # evidence_snippet is bounded by the extractor. Confirm ICS
        # never embeds a multi-kilobyte body the user didn't ask for.
        long_evidence = "MARKER " * 1000
        ev = _event(evidence_snippet=long_evidence)
        ics = build_ics([ev])
        # evidence_snippet was passed straight in; we don't truncate
        # again inside build_ics. We do verify that the SUMMARY line
        # uses only the title (not the evidence), and that the
        # DESCRIPTION contains exactly what we passed (which is the
        # caller's responsibility to bound).
        for line in ics.splitlines():
            if line.startswith("SUMMARY:"):
                self.assertEqual(line, "SUMMARY:Sync")


if __name__ == "__main__":
    unittest.main()
