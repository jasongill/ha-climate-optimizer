"""Tests for the pure low-sawtooth control helpers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

_CONTROL_PATH = (
    Path(__file__).parents[1] / "custom_components" / "climate_optimizer" / "control.py"
)
_SPEC = spec_from_file_location("climate_optimizer_control", _CONTROL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CONTROL = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CONTROL
_SPEC.loader.exec_module(_CONTROL)

TemperatureTracker = _CONTROL.TemperatureTracker
ThermalLearner = _CONTROL.ThermalLearner
gentle_setpoint_offset = _CONTROL.gentle_setpoint_offset
projected_stop = _CONTROL.projected_stop
confidence_aware_stop = _CONTROL.confidence_aware_stop


class TemperatureTrackerTests(unittest.TestCase):
    def test_falling_room_projects_an_early_cooling_stop(self) -> None:
        tracker = TemperatureTracker()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        for minute, temperature in ((0, 75.2), (5, 74.9), (10, 74.6)):
            tracker.add(start + timedelta(minutes=minute), temperature)

        estimate = tracker.estimate()

        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertLess(estimate.slope_per_minute or 0, 0)
        self.assertLessEqual(estimate.projected_5m, 74.6)
        self.assertTrue(
            projected_stop(
                cooling=True,
                current=estimate.filtered,
                projected=estimate.projected_5m,
                target=74.5,
            )
        )

    def test_duplicate_sensor_timestamp_does_not_invent_a_trend(self) -> None:
        tracker = TemperatureTracker()
        timestamp = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        tracker.add(timestamp, 75.0)
        tracker.add(timestamp, 74.8)

        estimate = tracker.estimate()

        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertEqual(estimate.filtered, 74.8)
        self.assertIsNone(estimate.slope_per_minute)

    def test_single_spike_is_filtered_by_recent_median(self) -> None:
        tracker = TemperatureTracker()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        tracker.add(start, 74.8)
        tracker.add(start + timedelta(minutes=1), 78.0)
        tracker.add(start + timedelta(minutes=2), 74.9)

        estimate = tracker.estimate()

        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertEqual(estimate.filtered, 74.9)


class GentleSetpointTests(unittest.TestCase):
    def test_near_target_uses_two_degree_offset(self) -> None:
        self.assertEqual(gentle_setpoint_offset(0.5, 4.0), 2.0)

    def test_large_error_uses_configured_offset(self) -> None:
        self.assertEqual(gentle_setpoint_offset(3.0, 4.0), 4.0)

    def test_midrange_error_interpolates_without_exceeding_configuration(self) -> None:
        self.assertEqual(gentle_setpoint_offset(2.0, 4.0), 3.0)

    def test_small_configured_offset_is_never_increased(self) -> None:
        self.assertEqual(gentle_setpoint_offset(0.5, 1.0), 1.0)


class ProjectedStopTests(unittest.TestCase):
    def test_cooling_does_not_stop_on_a_rising_projection(self) -> None:
        self.assertFalse(
            projected_stop(
                cooling=True,
                current=74.7,
                projected=75.0,
                target=74.5,
            )
        )

    def test_heating_stops_before_projected_crossing(self) -> None:
        self.assertTrue(
            projected_stop(
                cooling=False,
                current=74.3,
                projected=74.6,
                target=74.5,
            )
        )

    def test_low_confidence_projection_does_not_stop_early(self) -> None:
        stopped, reason = confidence_aware_stop(
            cooling=True,
            current=74.3,
            projected=73.8,
            target=74.0,
            confidence=0.4,
            slope_per_minute=-0.1,
        )
        self.assertFalse(stopped)
        self.assertIn("confidence", reason)

    def test_measured_target_always_stops(self) -> None:
        stopped, reason = confidence_aware_stop(
            cooling=True,
            current=74.0,
            projected=75.0,
            target=74.0,
            confidence=0.0,
            slope_per_minute=0.1,
        )
        self.assertTrue(stopped)
        self.assertIn("measured", reason)

    def test_high_confidence_projection_stops_only_near_target(self) -> None:
        stopped, _ = confidence_aware_stop(
            cooling=True,
            current=74.4,
            projected=73.9,
            target=74.0,
            confidence=1.0,
            slope_per_minute=-0.1,
        )
        self.assertTrue(stopped)

        far_from_target, _ = confidence_aware_stop(
            cooling=True,
            current=75.0,
            projected=73.9,
            target=74.0,
            confidence=1.0,
            slope_per_minute=-0.2,
        )
        self.assertFalse(far_from_target)

    def test_projection_never_stops_against_live_trend(self) -> None:
        stopped, reason = confidence_aware_stop(
            cooling=True,
            current=74.2,
            projected=73.9,
            target=74.0,
            confidence=1.0,
            slope_per_minute=0.02,
        )
        self.assertFalse(stopped)
        self.assertIn("trend", reason)


class ThermalLearnerTests(unittest.TestCase):
    def test_learns_cooling_rate_by_fan_and_projects_it(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "low")
        learner.observe(start + timedelta(minutes=5), 75.5, "cool", "low")
        learner.observe(start + timedelta(minutes=10), 75.0, "cool", "low")

        projected, confidence = learner.project(
            current=75.0,
            mode="cool",
            fan_mode="low",
            live_slope=None,
        )

        self.assertLess(projected, 75.0)
        self.assertGreater(confidence, 0.0)

    def test_low_confidence_rate_is_phased_in_without_live_slope(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "low")
        learner.observe(start + timedelta(minutes=1), 75.5, "cool", "low")

        projected, confidence = learner.project(
            current=75.5, mode="cool", fan_mode="low", live_slope=None
        )

        self.assertEqual(confidence, 0.1)
        self.assertAlmostEqual(projected, 75.25)

    def test_heating_and_cooling_models_are_separate(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 70.0, "heat", "low")
        learner.observe(start + timedelta(minutes=5), 70.5, "heat", "low")

        cooling, confidence = learner.project(
            current=75.0, mode="cool", fan_mode="low", live_slope=None
        )

        self.assertEqual(cooling, 75.0)
        self.assertEqual(confidence, 0.0)

    def test_idle_sensor_move_reduces_confidence(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "low")
        learner.observe(start + timedelta(minutes=5), 75.5, "cool", "low")
        learner.observe(start + timedelta(minutes=10), 75.0, "cool", "low")
        before = learner.project(
            current=75.0, mode="cool", fan_mode="low", live_slope=None
        )[1]
        learner.observe(start + timedelta(minutes=11), 75.0, None, None)
        learner.observe(start + timedelta(minutes=12), 79.0, None, None)
        after = learner.project(
            current=79.0, mode="cool", fan_mode="low", live_slope=None
        )[1]

        self.assertLessEqual(after, before)
        self.assertEqual(learner.sensor_move_count, 1)

    def test_persistent_state_round_trip(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "mute")
        learner.observe(start + timedelta(minutes=5), 75.6, "cool", "mute")
        restored = ThermalLearner()
        restored.restore(learner.as_dict())

        projected, confidence = restored.project(
            current=75.6, mode="cool", fan_mode="mute", live_slope=None
        )
        self.assertLess(projected, 75.6)
        self.assertGreater(confidence, 0.0)

    def test_outdoor_bucket_uses_seasonal_rate(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "low", 94.0)
        learner.observe(start + timedelta(minutes=5), 75.75, "cool", "low", 94.0)

        hot_projection, _ = learner.project(
            current=75.75,
            mode="cool",
            fan_mode="low",
            live_slope=None,
            outdoor_temperature=94.0,
        )

        self.assertLess(hot_projection, 75.75)
        self.assertIn("cool:low:outdoor_90", learner.as_dict()["rates"])

    def test_unchanged_ticks_do_not_inflate_quantized_sensor_rate(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 76.0, "cool", "low")
        for seconds in (30, 60, 90, 120):
            learner.observe(start + timedelta(seconds=seconds), 76.0, "cool", "low")
        learner.observe(start + timedelta(minutes=5), 75.9, "cool", "low")

        learned = learner.as_dict()["rates"]["cool:low"]["rate"]
        self.assertAlmostEqual(learned, -0.02)

    def test_post_stop_drift_is_normalized_to_five_minutes(self) -> None:
        learner = ThermalLearner()
        start = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        learner.observe(start, 75.0, "cool", "low")
        learner.observe(start + timedelta(minutes=5), 74.5, "cool", "low")
        learner.observe(start + timedelta(minutes=6), 74.4, None, None)
        learner.observe(start + timedelta(minutes=16), 74.2, None, None)

        learned = learner.as_dict()["post_stop"]["cool"]["drift"]
        self.assertAlmostEqual(learned, -0.1)

    def test_restore_rejects_rate_with_wrong_mode_direction(self) -> None:
        learner = ThermalLearner()
        learner.restore(
            {
                "version": 1,
                "rates": {"cool:low": {"rate": 0.2, "samples": 100}},
                "post_stop": {},
            }
        )

        self.assertNotIn("cool:low", learner.as_dict()["rates"])


if __name__ == "__main__":
    unittest.main()
