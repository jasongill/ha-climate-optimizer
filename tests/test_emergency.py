"""Exercise coarse unit-sensor protection through the production controller."""

from datetime import datetime, timedelta, timezone
import unittest

import test_delivery as delivery
from test_delivery import HVACMode, state


class IndoorEmergencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delivery.DeliveryTests.setUp(self)
        self.device._outdoor_sensor = "sensor.outdoor"
        self.states["sensor.outdoor"] = state("65")
        self.states["sensor.room"] = state("unavailable")

    async def control(self, indoor):
        self.states["climate.split"].attributes["current_temperature"] = indoor
        await self.device._async_control()

    async def test_cold_unit_reading_heats_with_mild_outdoor_temperature(self):
        await self.control(54)
        self.assertEqual(self.device._active_mode, HVACMode.HEAT)
        self.assertTrue(self.device._emergency_active)
        self.assertEqual(self.device._last_pushed_setpoint, 62)
        self.assertIn("unit indoor safety guard", self.device._decision_reason)
        self.assertEqual(self.device._recovery.offset, 0)

    async def test_hot_unit_reading_cools_without_outdoor_sensor(self):
        self.device._outdoor_sensor = None
        await self.control(86)
        self.assertEqual(self.device._active_mode, HVACMode.COOL)
        self.assertEqual(self.device._last_pushed_setpoint, 80)

    async def test_recent_unchanged_room_reports_eventually_trigger_guard(self):
        room = state("67")
        room.last_reported = datetime.now(timezone.utc)
        room.last_changed -= timedelta(hours=13)
        self.states["sensor.room"] = room
        await self.control(54)
        self.assertEqual(self.device._active_mode, HVACMode.HEAT)
        self.assertIn("stuck", self.device._decision_reason)

    async def test_stale_room_reports_trigger_guard(self):
        room = state("67")
        room.last_reported = datetime.now(timezone.utc) - timedelta(hours=2)
        self.states["sensor.room"] = room
        await self.control(86)
        self.assertEqual(self.device._active_mode, HVACMode.COOL)
        self.assertIn("stale", self.device._decision_reason)

    async def test_heat_guard_holds_until_five_degree_recovery(self):
        await self.control(55)
        await self.control(58)
        self.assertEqual(self.device._active_mode, HVACMode.HEAT)
        await self.control(60)
        self.assertIsNone(self.device._active_mode)
        self.assertIsNone(self.device._emergency_indoor_mode)
        self.assertFalse(self.device._emergency_active)

    async def test_cool_guard_holds_until_five_degree_recovery(self):
        await self.control(85)
        await self.control(82)
        self.assertEqual(self.device._active_mode, HVACMode.COOL)
        await self.control(80)
        self.assertIsNone(self.device._active_mode)
        self.assertIsNone(self.device._emergency_indoor_mode)

    async def test_indoor_guard_wins_over_opposing_outdoor_fallback(self):
        self.states["sensor.outdoor"].state = "20"
        await self.control(86)
        self.assertEqual(self.device._active_mode, HVACMode.COOL)

    async def test_unavailable_or_invalid_indoor_reading_keeps_outdoor_fallback(self):
        for value in (None, "unknown", "unavailable", "bad", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.setUp()
                self.states["sensor.outdoor"].state = "20"
                await self.control(value)
                self.assertEqual(self.device._active_mode, HVACMode.HEAT)
                self.assertIn("outdoor fallback", self.device._decision_reason)

    async def test_old_downstream_reports_are_not_used_as_indoor_guard(self):
        self.states["climate.split"].last_reported = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        )
        await self.control(54)
        self.assertIsNone(self.device._active_mode)

    async def test_room_recovery_restores_normal_control_and_clears_guard(self):
        await self.control(54)
        self.states["sensor.room"] = state("70")
        self.device._last_transition -= timedelta(minutes=10)
        await self.control(54)
        self.assertFalse(self.device._emergency_active)
        self.assertIsNone(self.device._emergency_indoor_mode)
        self.assertIsNone(self.device._active_mode)

    async def test_healthy_room_sensor_does_not_use_unit_sensor_for_control(self):
        self.states["sensor.room"] = state("70")
        await self.control(54)
        self.assertIsNone(self.device._active_mode)
        self.assertIsNone(self.device._emergency_indoor_mode)

    async def test_disabling_guard_preserves_existing_outdoor_fallback(self):
        self.device._emergency_indoor_enable = False
        await self.control(54)
        self.assertIsNone(self.device._active_mode)
        self.states["sensor.outdoor"].state = "20"
        await self.control(54)
        self.assertEqual(self.device._active_mode, HVACMode.HEAT)

    async def test_emergency_disabled_and_off_mode_never_activate_guard(self):
        self.device._emergency_enable = False
        await self.control(54)
        self.assertIsNone(self.device._active_mode)
        self.device._emergency_enable = True
        self.device._attr_hvac_mode = HVACMode.OFF
        await self.control(54)
        self.assertIsNone(self.device._active_mode)

    async def test_disallowed_guard_mode_blocks_opposing_outdoor_command(self):
        self.device._attr_hvac_mode = HVACMode.HEAT
        self.states["sensor.outdoor"].state = "20"
        await self.control(86)
        self.assertIsNone(self.device._active_mode)
        self.assertIn("required mode is not enabled", self.device._decision_reason)

    async def test_opposing_active_mode_stops_during_transition_hold(self):
        self.device._active_mode = HVACMode.HEAT
        self.states["climate.split"].state = "heat"
        self.device._last_transition = datetime.now(timezone.utc)
        await self.control(86)
        self.assertIsNone(self.device._active_mode)
        self.assertIn("Min cycle hold", self.device._decision_reason)
        self.assertTrue(any(
            c.args[1] == "set_hvac_mode" and c.args[2]["hvac_mode"] == "off"
            for c in self.call.call_args_list
        ))

    async def test_custom_thresholds_are_used(self):
        self.device._emergency_heat_below_indoor = 50
        await self.control(54)
        self.assertIsNone(self.device._active_mode)
        await self.control(49)
        self.assertEqual(self.device._active_mode, HVACMode.HEAT)


if __name__ == "__main__":
    unittest.main()
