from __future__ import annotations

import unittest
from typing import Any, Mapping, Optional

from wolf.core.types import RiskLevel
from wolf.fakes.robot import FakeRobotTransport
from wolf.safety.robot_preflight import (
    DEFAULT_ENVIRONMENT_RISK_KEYS,
    PreflightConfig,
    PreflightDecision,
    PreflightInput,
    RobotPreflight,
)


def _healthy_environment() -> dict:
    return {key: False for key in DEFAULT_ENVIRONMENT_RISK_KEYS}


def _healthy_input(**overrides: Any) -> PreflightInput:
    base: Mapping[str, Any] = dict(
        battery_pct=80.0,
        latency_ms=20.0,
        sensor_status={"imu": "healthy", "wheel_odom": "healthy"},
        lidar_status="healthy",
        e_stop=False,
        manual_override=False,
        environment_risk=_healthy_environment(),
        action_classification=RiskLevel.LOW,
    )
    merged: dict = dict(base)
    merged.update(overrides)
    return PreflightInput(**merged)


def _has_check(decision: PreflightDecision, needle: str) -> bool:
    return any(needle in c for c in decision.failed_checks)


class HealthyAndClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_healthy_low_action_is_allowed(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=RiskLevel.LOW)
        )
        self.assertTrue(d.allowed)
        self.assertFalse(d.requires_confirmation)
        self.assertEqual(d.failed_checks, ())
        self.assertIs(d.risk_level, RiskLevel.LOW)

    def test_healthy_medium_action_is_allowed(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=RiskLevel.MEDIUM)
        )
        self.assertTrue(d.allowed)
        self.assertFalse(d.requires_confirmation)

    def test_high_action_requires_confirmation(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=RiskLevel.HIGH)
        )
        self.assertTrue(d.allowed)
        self.assertTrue(d.requires_confirmation)
        self.assertIn("confirmation", d.reason)

    def test_critical_action_is_denied(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=RiskLevel.CRITICAL)
        )
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "action_classification"))

    def test_unknown_classification_is_denied(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=None)
        )
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "action_classification"))


class BatteryCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_battery_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(battery_pct=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "battery"))

    def test_battery_below_threshold_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(battery_pct=10.0))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "battery"))

    def test_battery_at_threshold_is_allowed(self) -> None:
        # Sanity check the >= boundary
        config = PreflightConfig(min_battery_pct=30.0)
        preflight = RobotPreflight(config=config)
        d = preflight.evaluate(_healthy_input(battery_pct=30.0))
        self.assertTrue(d.allowed)

    def test_battery_threshold_is_configurable(self) -> None:
        strict = RobotPreflight(config=PreflightConfig(min_battery_pct=90.0))
        d = strict.evaluate(_healthy_input(battery_pct=80.0))
        self.assertFalse(d.allowed)


class LatencyCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_latency_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(latency_ms=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "network_latency"))

    def test_latency_above_threshold_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(latency_ms=500.0))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "network_latency"))

    def test_latency_threshold_is_configurable(self) -> None:
        strict = RobotPreflight(config=PreflightConfig(max_latency_ms=10.0))
        d = strict.evaluate(_healthy_input(latency_ms=50.0))
        self.assertFalse(d.allowed)


class SensorCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_sensor_status_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(sensor_status=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "sensors"))

    def test_required_sensor_missing_is_denied(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(sensor_status={"wheel_odom": "healthy"})
        )
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "imu"))

    def test_required_sensor_unhealthy_is_denied(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(
                sensor_status={"imu": "degraded", "wheel_odom": "healthy"}
            )
        )
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "imu"))
        self.assertTrue(_has_check(d, "degraded"))

    def test_required_sensor_list_is_configurable(self) -> None:
        config = PreflightConfig(required_sensors=frozenset({"camera"}))
        preflight = RobotPreflight(config=config)
        d = preflight.evaluate(
            _healthy_input(sensor_status={"imu": "healthy"})
        )
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "camera"))


class LidarCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_lidar_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(lidar_status=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "lidar"))

    def test_unhealthy_lidar_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(lidar_status="unhealthy"))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "lidar"))

    def test_disabled_lidar_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(lidar_status="disabled"))
        self.assertFalse(d.allowed)

    def test_stale_lidar_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(lidar_status="stale"))
        self.assertFalse(d.allowed)


class EmergencyStopCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_estop_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(e_stop=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "emergency_stop"))

    def test_engaged_estop_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(e_stop=True))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "emergency_stop"))


class ManualOverrideCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_unknown_manual_override_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(manual_override=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "manual_override"))

    def test_active_manual_override_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(manual_override=True))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "manual_override"))


class EnvironmentCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_each_environment_risk_key_triggers_denial(self) -> None:
        for key in DEFAULT_ENVIRONMENT_RISK_KEYS:
            with self.subTest(key=key):
                env = _healthy_environment()
                env[key] = True
                d = self.preflight.evaluate(
                    _healthy_input(environment_risk=env)
                )
                self.assertFalse(
                    d.allowed, f"{key}=True should deny but allowed"
                )
                self.assertTrue(
                    _has_check(d, key),
                    f"failed_checks should mention {key!r}",
                )

    def test_missing_environment_map_is_denied(self) -> None:
        d = self.preflight.evaluate(_healthy_input(environment_risk=None))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "environment"))

    def test_environment_risk_keys_are_extensible(self) -> None:
        custom = (*DEFAULT_ENVIRONMENT_RISK_KEYS, "near_radiation")
        config = PreflightConfig(environment_risk_keys=custom)
        preflight = RobotPreflight(config=config)
        env = _healthy_environment()
        env["near_radiation"] = True
        d = preflight.evaluate(_healthy_input(environment_risk=env))
        self.assertFalse(d.allowed)
        self.assertTrue(_has_check(d, "near_radiation"))


class CompositeFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_multiple_failures_are_all_listed(self) -> None:
        env = _healthy_environment()
        env["near_people"] = True
        d = self.preflight.evaluate(
            _healthy_input(
                battery_pct=5.0,
                latency_ms=999.0,
                e_stop=True,
                manual_override=True,
                lidar_status="unhealthy",
                environment_risk=env,
                action_classification=RiskLevel.CRITICAL,
            )
        )
        self.assertFalse(d.allowed)
        joined = " | ".join(d.failed_checks)
        for needle in (
            "battery",
            "network_latency",
            "emergency_stop",
            "manual_override",
            "lidar",
            "near_people",
            "action_classification",
        ):
            self.assertIn(needle, joined, f"missing {needle!r} in {joined!r}")
        self.assertGreaterEqual(len(d.failed_checks), 7)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preflight = RobotPreflight()

    def test_snapshot_contains_only_known_summary_keys(self) -> None:
        d = self.preflight.evaluate(_healthy_input())
        self.assertSetEqual(
            set(d.snapshot.keys()),
            {
                "battery_pct",
                "latency_ms",
                "lidar_status",
                "e_stop",
                "manual_override",
                "required_sensor_status",
                "environment_risks",
                "action_classification",
            },
        )

    def test_snapshot_does_not_echo_unknown_environment_keys(self) -> None:
        env = _healthy_environment()
        env["evil_secret_key"] = "leaky_value"
        d = self.preflight.evaluate(_healthy_input(environment_risk=env))
        flat = repr(d.snapshot)
        self.assertNotIn("evil_secret_key", flat)
        self.assertNotIn("leaky_value", flat)

    def test_snapshot_does_not_echo_unknown_sensor_keys(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(
                sensor_status={
                    "imu": "healthy",
                    "wheel_odom": "healthy",
                    "raw_camera_frame_b64": "AAAAAAAA",
                }
            )
        )
        flat = repr(d.snapshot)
        self.assertNotIn("raw_camera_frame_b64", flat)
        self.assertNotIn("AAAAAAAA", flat)

    def test_snapshot_serializes_risk_level_as_string(self) -> None:
        d = self.preflight.evaluate(
            _healthy_input(action_classification=RiskLevel.MEDIUM)
        )
        self.assertEqual(d.snapshot["action_classification"], "medium")


class FakeRobotTransportIntegrationTest(unittest.TestCase):
    def test_preflight_input_from_fake_get_state(self) -> None:
        fake = FakeRobotTransport()
        state = fake.get_state()
        preflight_input = PreflightInput.from_robot_state(
            state,
            action_classification=RiskLevel.LOW,
            environment_risk=_healthy_environment(),
        )
        decision = RobotPreflight().evaluate(preflight_input)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.failed_checks, ())

    def test_preflight_input_carries_estop_from_fake(self) -> None:
        fake = FakeRobotTransport()
        fake.emergency_stop()
        state = fake.get_state()
        self.assertTrue(state.e_stop)
        preflight_input = PreflightInput.from_robot_state(
            state,
            action_classification=RiskLevel.LOW,
            environment_risk=_healthy_environment(),
        )
        decision = RobotPreflight().evaluate(preflight_input)
        self.assertFalse(decision.allowed)
        self.assertTrue(_has_check(decision, "emergency_stop"))


if __name__ == "__main__":
    unittest.main()
