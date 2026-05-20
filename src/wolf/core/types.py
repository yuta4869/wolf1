from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


@dataclass(frozen=True)
class Action:
    kind: str
    risk: RiskLevel
    target: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditEvent:
    ts: str
    actor: str
    action_kind: str
    decision: str
    target: str
    outcome: str
    detail: Mapping[str, Any] = field(default_factory=dict)
