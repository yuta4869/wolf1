from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class RobotState:
    battery_pct: float
    latency_ms: float
    sensor_health: bool
    lidar_health: bool
    e_stop: bool
    manual_override: bool


@runtime_checkable
class RobotTransport(Protocol):
    def get_state(self) -> RobotState: ...

    def execute_motion(
        self, *, command: Mapping[str, Any], confirmation_token: str
    ) -> None: ...

    def emergency_stop(self) -> None: ...
