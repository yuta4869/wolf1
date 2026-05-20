from __future__ import annotations


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
