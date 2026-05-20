from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, List, Mapping, Optional, Tuple

from ..adapters.robot_transport import RobotState
from ..core.types import RiskLevel


DEFAULT_ENVIRONMENT_RISK_KEYS: Tuple[str, ...] = (
    "near_people",
    "near_animals",
    "near_fragile",
    "near_water",
    "near_fire",
    "near_stairs",
    "near_chemicals",
    "unstable_floor",
    "unknown_obstacle",
)

DEFAULT_REQUIRED_SENSORS: FrozenSet[str] = frozenset({"imu", "wheel_odom"})
DEFAULT_HEALTHY_SENSOR_STATES: FrozenSet[str] = frozenset({"healthy"})
DEFAULT_HEALTHY_LIDAR_STATES: FrozenSet[str] = frozenset({"healthy"})


@dataclass(frozen=True)
class PreflightConfig:
    min_battery_pct: float = 30.0
    max_latency_ms: float = 100.0
    required_sensors: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_REQUIRED_SENSORS
    )
    healthy_sensor_states: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_HEALTHY_SENSOR_STATES
    )
    healthy_lidar_states: FrozenSet[str] = field(
        default_factory=lambda: DEFAULT_HEALTHY_LIDAR_STATES
    )
    environment_risk_keys: Tuple[str, ...] = DEFAULT_ENVIRONMENT_RISK_KEYS


@dataclass(frozen=True)
class PreflightInput:
    battery_pct: Optional[float] = None
    latency_ms: Optional[float] = None
    sensor_status: Optional[Mapping[str, str]] = None
    lidar_status: Optional[str] = None
    e_stop: Optional[bool] = None
    manual_override: Optional[bool] = None
    environment_risk: Optional[Mapping[str, bool]] = None
    action_classification: Optional[RiskLevel] = None

    @classmethod
    def from_robot_state(
        cls,
        state: RobotState,
        *,
        action_classification: Optional[RiskLevel],
        environment_risk: Optional[Mapping[str, bool]],
        sensor_status: Optional[Mapping[str, str]] = None,
        lidar_status: Optional[str] = None,
    ) -> "PreflightInput":
        if sensor_status is None and state.sensor_health:
            sensor_status = {"imu": "healthy", "wheel_odom": "healthy"}
        if lidar_status is None:
            lidar_status = "healthy" if state.lidar_health else "unhealthy"
        return cls(
            battery_pct=state.battery_pct,
            latency_ms=state.latency_ms,
            sensor_status=sensor_status,
            lidar_status=lidar_status,
            e_stop=state.e_stop,
            manual_override=state.manual_override,
            environment_risk=environment_risk,
            action_classification=action_classification,
        )


@dataclass(frozen=True)
class PreflightDecision:
    allowed: bool
    reason: str
    failed_checks: Tuple[str, ...]
    warnings: Tuple[str, ...]
    risk_level: Optional[RiskLevel]
    requires_confirmation: bool
    snapshot: Mapping[str, Any]


class RobotPreflight:
    def __init__(self, config: Optional[PreflightConfig] = None) -> None:
        self.config: PreflightConfig = (
            config if config is not None else PreflightConfig()
        )

    def evaluate(self, input: PreflightInput) -> PreflightDecision:
        failed: List[str] = []
        warnings: List[str] = []
        requires_confirmation = False

        self._check_battery(input, failed)
        self._check_latency(input, failed)
        self._check_sensors(input, failed)
        self._check_lidar(input, failed)
        self._check_emergency_stop(input, failed)
        self._check_manual_override(input, failed)
        self._check_environment(input, failed)
        requires_confirmation = self._check_action_classification(input, failed)

        snapshot = self._snapshot(input)

        if failed:
            return PreflightDecision(
                allowed=False,
                reason=f"{len(failed)} preflight check(s) failed (fail-closed)",
                failed_checks=tuple(failed),
                warnings=tuple(warnings),
                risk_level=input.action_classification,
                requires_confirmation=False,
                snapshot=snapshot,
            )

        return PreflightDecision(
            allowed=True,
            reason=(
                "all preflight checks passed; confirmation required"
                if requires_confirmation
                else "all preflight checks passed"
            ),
            failed_checks=(),
            warnings=tuple(warnings),
            risk_level=input.action_classification,
            requires_confirmation=requires_confirmation,
            snapshot=snapshot,
        )

    def _check_battery(self, i: PreflightInput, failed: List[str]) -> None:
        if i.battery_pct is None:
            failed.append("battery: unknown")
            return
        if i.battery_pct < self.config.min_battery_pct:
            failed.append(
                f"battery: {i.battery_pct:.1f}% "
                f"below threshold {self.config.min_battery_pct:.1f}%"
            )

    def _check_latency(self, i: PreflightInput, failed: List[str]) -> None:
        if i.latency_ms is None:
            failed.append("network_latency: unknown")
            return
        if i.latency_ms > self.config.max_latency_ms:
            failed.append(
                f"network_latency: {i.latency_ms:.1f}ms "
                f"above threshold {self.config.max_latency_ms:.1f}ms"
            )

    def _check_sensors(self, i: PreflightInput, failed: List[str]) -> None:
        if i.sensor_status is None:
            failed.append("sensors: unknown")
            return
        for name in self.config.required_sensors:
            state = i.sensor_status.get(name)
            if state is None:
                failed.append(f"sensors: required sensor {name!r} missing")
            elif state not in self.config.healthy_sensor_states:
                failed.append(
                    f"sensors: required sensor {name!r} status {state!r}"
                )

    def _check_lidar(self, i: PreflightInput, failed: List[str]) -> None:
        if i.lidar_status is None:
            failed.append("lidar: unknown")
            return
        if i.lidar_status not in self.config.healthy_lidar_states:
            failed.append(f"lidar: status {i.lidar_status!r}")

    def _check_emergency_stop(
        self, i: PreflightInput, failed: List[str]
    ) -> None:
        if i.e_stop is None:
            failed.append("emergency_stop: unknown")
            return
        if i.e_stop is True:
            failed.append("emergency_stop: engaged")

    def _check_manual_override(
        self, i: PreflightInput, failed: List[str]
    ) -> None:
        if i.manual_override is None:
            failed.append("manual_override: unknown")
            return
        if i.manual_override is True:
            failed.append("manual_override: active")

    def _check_environment(
        self, i: PreflightInput, failed: List[str]
    ) -> None:
        if i.environment_risk is None:
            failed.append("environment: unknown")
            return
        for key in self.config.environment_risk_keys:
            if bool(i.environment_risk.get(key)):
                failed.append(f"environment: {key} present")

    def _check_action_classification(
        self, i: PreflightInput, failed: List[str]
    ) -> bool:
        cls_ = i.action_classification
        if cls_ is None:
            failed.append("action_classification: unknown")
            return False
        if cls_ is RiskLevel.CRITICAL:
            failed.append("action_classification: CRITICAL")
            return False
        if cls_ is RiskLevel.HIGH:
            return True
        return False

    def _snapshot(self, i: PreflightInput) -> Mapping[str, Any]:
        if i.sensor_status is None:
            sensor_summary: Optional[Mapping[str, str]] = None
        else:
            sensor_summary = {
                name: i.sensor_status.get(name, "missing")
                for name in self.config.required_sensors
            }

        if i.environment_risk is None:
            env_summary: Optional[Mapping[str, bool]] = None
        else:
            env_summary = {
                key: bool(i.environment_risk.get(key))
                for key in self.config.environment_risk_keys
            }

        return {
            "battery_pct": i.battery_pct,
            "latency_ms": i.latency_ms,
            "lidar_status": i.lidar_status,
            "e_stop": i.e_stop,
            "manual_override": i.manual_override,
            "required_sensor_status": sensor_summary,
            "environment_risks": env_summary,
            "action_classification": (
                i.action_classification.value
                if i.action_classification is not None
                else None
            ),
        }
