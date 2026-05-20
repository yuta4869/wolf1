from __future__ import annotations

from dataclasses import replace
from typing import Any, List, Mapping, Optional

from ..adapters.robot_transport import RobotState
from ..core.errors import SafetyViolation


HEALTHY_STATE = RobotState(
    battery_pct=85.0,
    latency_ms=15.0,
    sensor_health=True,
    lidar_health=True,
    e_stop=False,
    manual_override=False,
)


class FakeRobotTransport:
    def __init__(self, state: Optional[RobotState] = None) -> None:
        self._state = state if state is not None else HEALTHY_STATE
        self.executed: List[Mapping[str, Any]] = []
        self.estop_count = 0

    def get_state(self) -> RobotState:
        return self._state

    def set_state(self, state: RobotState) -> None:
        self._state = state

    def execute_motion(
        self, *, command: Mapping[str, Any], confirmation_token: str
    ) -> None:
        if not confirmation_token:
            raise PermissionError("confirmation_token required")
        if self._state.e_stop:
            raise SafetyViolation("emergency stop active")
        if self._state.manual_override:
            raise SafetyViolation("manual override active")
        self.executed.append(dict(command))

    def emergency_stop(self) -> None:
        self.estop_count += 1
        self._state = replace(self._state, e_stop=True)
