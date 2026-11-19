"""Replay stalled rooms and verify recovery through the production controller."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from test_control import _CONTROL
import test_delivery as delivery
from test_delivery import HVACMode, NS, state


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.recovery = _CONTROL.RecoveryCompensator()
        self.start = datetime(2026, 11, 17, tzinfo=timezone.utc)

    def update(self, minute, **kwargs):
        params = dict(
            mode="heat",
            target=67.0,
            current=66.0,
            max_offset=3.0,
            delay_minutes=25.0,
            confirmed=True,
        )
        params.update(kwargs)
        return self.recovery.update(self.start + timedelta(minutes=minute), **params)

    def replay(self, end, **kwargs):
        return [self.update(i, **kwargs) for i in range(end + 1)]

    def test_stalled_room_ramps_by_elapsed_time_and_caps(self):
        outputs = self.replay(150)
        self.assertEqual(outputs[24], 0)
        self.assertEqual(outputs[25], 0.5)
        self.assertEqual(outputs[34], 0.5)
        self.assertEqual(outputs[35], 1)
        self.assertEqual(outputs[-1], 3)
        for _ in range(100):
            self.assertEqual(self.update(150), 3)

    def test_event_frequency_does_not_accelerate_recovery(self):
        for tick in range(25 * 60 + 1):
            result = self.recovery.update(
                self.start + timedelta(seconds=tick),
                mode="heat",
                target=67,
                current=66,
                max_offset=3,
                delay_minutes=25,
                confirmed=True,
            )
        self.assertEqual(result, 0.5)

    def test_cooling_uses_symmetric_room_progress(self):
        self.assertEqual(self.replay(35, mode="cool", target=74, current=75)[-1], 1)
        self.assertEqual(self.update(36, mode="cool", target=74, current=74.7), 0.5)
        self.assertEqual(self.update(37, mode="cool", target=74, current=74), 0)

    def test_improving_room_and_brief_pause_do_not_boost(self):
        for i in range(61):
            self.assertEqual(self.update(i, current=64 + i * 0.02), 0)
        # Ten minutes of a flat reading is not a stalled recovery window.
        for i in range(61, 71):
            self.assertEqual(self.update(i, current=65.2), 0)

    def test_backoff_taper_and_target_reset(self):
        self.replay(65)
        self.assertEqual(self.update(66, current=66.3), 2)
        self.assertAlmostEqual(self.update(67, current=66.6), 1.2)
        self.assertLess(self.update(68, current=66.9), 0.3)
        self.assertEqual(self.update(69, current=67), 0)
        self.assertEqual(self.recovery.offset, 0)

    def test_unconfirmed_mode_and_disabled_recovery_never_boost(self):
        self.assertEqual(max(self.replay(90, confirmed=False)), 0)
        self.assertEqual(max(self.replay(90, max_offset=0)), 0)

    def test_resets_on_gap_clock_reversal_mode_target_and_invalid_input(self):
        for kwargs, minute in [
            ({}, 50),
            ({}, 10),
            ({"mode": "cool", "current": 68}, 36),
            ({"target": 68}, 36),
            ({"current": float("nan")}, 36),
            ({"confirmed": False}, 36),
        ]:
            with self.subTest(kwargs=kwargs, minute=minute):
                self.setUp()
                self.replay(35)
                self.assertEqual(self.update(minute, **kwargs), 0)
                self.assertEqual(self.recovery.offset, 0)

    def test_actuator_headroom_reduces_accumulator(self):
        self.replay(65)
        self.assertEqual(self.update(66, max_offset=0.25), 0.25)
        self.assertEqual(self.recovery.offset, 0.25)
        self.assertEqual(self.update(67, max_offset=0), 0)


class RecoveryControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delivery.DeliveryTests.setUp(self)
        self.start = datetime(2026, 11, 17, tzinfo=timezone.utc)
        self.device._heat_target = 67
        self.device._recovery_max_offset = 3
        self.device._async_send = AsyncMock()
        self.states["climate.split"] = state(
            "heat",
            temperature=69,
            current_temperature=73,
            fan_mode="low",
            fan_modes=["low", "medium", "high"],
            min_temp=60,
            max_temp=90,
        )

    async def drive(self, minute, current=66, mode=HVACMode.HEAT):
        now = self.start + timedelta(minutes=minute)
        with patch.object(NS["dt_util"], "utcnow", return_value=now):
            return await self.device._async_drive_active(
                current, mode, self.states["climate.split"]
            )

    async def prime(self):
        for minute in range(66):
            await self.drive(minute)

    async def test_stall_increases_command_without_changing_fan_or_target(self):
        await self.prime()
        args = self.device._async_send.call_args.args
        self.assertEqual(args[2], 72)
        self.assertEqual(args[3], "low")
        self.assertEqual(self.device._heat_target, 67)
        self.assertEqual(self.device.extra_state_attributes["recovery_offset"], 2.5)
        self.assertIn("Recovery +2.5", await self.drive(65))

    async def test_default_disabled_preserves_existing_command(self):
        self.device._recovery_max_offset = 0
        await self.prime()
        self.assertEqual(self.device._async_send.call_args.args[2], 69)

    async def test_cooling_subtracts_boost(self):
        self.states["climate.split"].state = "cool"
        for minute in range(36):
            await self.drive(minute, current=73, mode=HVACMode.COOL)
        self.assertEqual(self.device._async_send.call_args.args[2], 69)

    async def test_downstream_limits_bound_recovery(self):
        self.states["climate.split"].attributes["max_temp"] = 70
        await self.prime()
        self.assertEqual(self.device._async_send.call_args.args[2], 70)
        self.assertEqual(self.device._recovery.offset, 1)

    async def test_stop_and_target_change_clear_recovery(self):
        await self.prime()
        await self.device._async_stop_downstream()
        self.assertEqual(self.device._recovery.offset, 0)
        await self.prime()
        self.device._async_control = AsyncMock()
        await self.device.async_set_temperature(target_temp_low=68)
        self.assertEqual(self.device._recovery.offset, 0)

    async def test_off_mode_sensor_loss_and_disconnect_clear_recovery(self):
        for cause in ("off", "unavailable_sensor", "stale_sensor", "downstream"):
            with self.subTest(cause=cause):
                self.states["climate.split"].state = "heat"
                self.states["sensor.room"] = state("66")
                self.device._attr_hvac_mode = HVACMode.HEAT
                await self.prime()
                if cause == "off":
                    self.device._attr_hvac_mode = HVACMode.OFF
                elif cause == "unavailable_sensor":
                    self.states["sensor.room"].state = "unavailable"
                elif cause == "stale_sensor":
                    self.states["sensor.room"].last_updated -= timedelta(hours=2)
                else:
                    self.states["climate.split"].state = "unavailable"
                await self.device._async_control()
                self.assertEqual(self.device._recovery.offset, 0)

    async def test_unconfirmed_and_startup_settling_do_not_accumulate(self):
        self.states["climate.split"].state = "off"
        await self.prime()
        self.assertEqual(self.device._recovery.offset, 0)
        self.states["climate.split"].state = "heat"
        self.device._active_mode = HVACMode.HEAT
        self.device._last_transition = self.start + timedelta(minutes=64)
        await self.drive(65)
        self.assertEqual(self.device._recovery.offset, 0)

    async def test_reaching_target_still_stops_with_recovery(self):
        await self.prime()
        self.device._active_mode = HVACMode.HEAT
        self.states["sensor.room"] = state("67")
        await self.device._async_control()
        self.assertIsNone(self.device._active_mode)
        self.assertEqual(self.device._recovery.offset, 0)


if __name__ == "__main__":
    unittest.main()
