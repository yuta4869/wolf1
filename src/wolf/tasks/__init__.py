"""Local-first task / calendar extraction.

This package converts the text of one mail (local `.eml` / `.mbox`,
Maildir, or Gmail) into `TaskCandidate` and `CalendarEventCandidate`
records, and emits a draft iCalendar (.ics) file when asked.

Design constraints:

- No Google Calendar API integration. No `gmail.send`. No SMTP /
  IMAP. No calendar push. The CLI produces *candidates* and *draft
  .ics text*; the user is responsible for moving them into a
  real calendar.
- LLM-driven extraction goes through the existing Router (mail
  body wrapped as `UntrustedText(SourceKind.EMAIL)`, mail-strict
  prompt-injection scan).
- When the LLM's output is not valid JSON, the extractor falls
  back to a deterministic regex heuristic so the fake LLM (which
  emits no JSON) still produces useful candidates in tests and
  smoke runs.
- Bodies, raw prompts, and full LLM outputs never appear in the
  audit log. Only metadata counts are recorded by the CLI layer.
"""

from .extract import (
    EXTRACTION_INSTRUCTION,
    ExtractionResult,
    extract_candidates,
    extract_candidates_from_text,
)
from .ics import build_ics
from .types import CalendarEventCandidate, TaskCandidate

__all__ = [
    "CalendarEventCandidate",
    "EXTRACTION_INSTRUCTION",
    "ExtractionResult",
    "TaskCandidate",
    "build_ics",
    "extract_candidates",
    "extract_candidates_from_text",
]
