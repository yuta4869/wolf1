from __future__ import annotations

from typing import Optional


class WolfError(Exception):
    pass


class PolicyDenied(WolfError):
    pass


class ConfirmationRequired(WolfError):
    pass


class FailClosed(WolfError):
    pass


class SafetyViolation(WolfError):
    pass


class AdapterError(WolfError):
    """Raised by external-system adapters (LLM / mail / robot transport /
    file index / etc.) when the underlying service fails or returns an
    unexpected response.

    Carries a short label (safe for logging) and an optional cause. The
    string form deliberately does NOT echo the prompt or request body;
    callers are responsible for separate audit logging if richer context
    is needed.
    """

    def __init__(self, label: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(label)
        self.label = label
        self.cause = cause

    def __repr__(self) -> str:
        cause_name = type(self.cause).__name__ if self.cause is not None else "None"
        return f"AdapterError(label={self.label!r}, cause={cause_name})"
