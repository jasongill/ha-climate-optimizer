"""Exercise the real climate controller with lightweight HA boundary doubles.

The repository's tests run without Home Assistant installed. Strip only HA and
package imports, then execute the complete production module against doubles;
no controller methods are reimplemented here.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_control import _CONTROL

ROOT = Path(__file__).parents[1] / "custom_components" / "climate_optimizer"


class HVACMode(str, Enum):
    OFF = "off"
    HEAT_COOL = "heat_cool"
    HEAT = "heat"
    COOL = "cool"


class HVACAction(str, Enum):
    OFF = "off"
    IDLE = "idle"
    COOLING = "cooling"
    HEATING = "heating"


class ClimateEntity:
    pass


class RestoreEntity:
    pass


NS = runpy.run_path(str(ROOT / "const.py"))
NS.update(
    __name__="delivery_test_climate",
    HVACMode=HVACMode,
    HVACAction=HVACAction,
    ClimateEntity=ClimateEntity,
    RestoreEntity=RestoreEntity,
    ClimateEntityFeature=SimpleNamespace(
        TARGET_TEMPERATURE_RANGE=1, FAN_MODE=2, TURN_ON=4, TURN_OFF=8
    ),
    UnitOfTemperature=SimpleNamespace(FAHRENHEIT="°F"),
    DeviceInfo=dict,
    callback=lambda f: f,
    CONF_NAME="name",
    STATE_UNAVAILABLE="unavailable",
    STATE_UNKNOWN="unknown",
    dt_util=SimpleNamespace(
        utcnow=lambda: datetime.now(timezone.utc),
        parse_datetime=lambda s: datetime.fromisoformat(s) if s else None,
    ),
)
for attr in (
    "FAN_MODE",
    "FAN_MODES",
    "HVAC_MODE",
    "MAX_TEMP",
    "MIN_TEMP",
    "TARGET_TEMP_STEP",
    "TEMPERATURE",
):
    NS["ATTR_" + attr] = attr.lower()
for name in (
    "ThermalLearner",
    "TemperatureTracker",
    "confidence_aware_stop",
    "gentle_setpoint_offset",
):
    NS[name] = getattr(_CONTROL, name)
tree = ast.parse((ROOT / "climate.py").read_text())
tree.body = [
    node
    for node in tree.body
    if not (
        isinstance(node, ast.ImportFrom)
        and (node.level or (node.module or "").startswith("homeassistant"))
    )
]
exec(compile(tree, str(ROOT / "climate.py"), "exec"), NS)
Controller = NS["VirtualClimateDevice"]


def state(mode, **attributes):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        state=mode, attributes=attributes, last_changed=now, last_updated=now
    )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 1000.0
        self.patch = patch.dict(NS, monotonic=lambda: self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.device = Controller(
            SimpleNamespace(entry_id="garage", options={}),
            {
                "name": "Garage",
                "source_temp_sensor": "sensor.room",
                "downstream_climate": "climate.split",
                "cool_target": 72,
            },
        )
        self.states = {
            "climate.split": state(
                "off",
                temperature=70,
                fan_mode="low",
                fan_modes=["low", "medium", "high"],
                min_temp=60,
                max_temp=90,
            ),
            "sensor.room": state("76"),
        }
        self.device.hass = SimpleNamespace(
            states=SimpleNamespace(get=self.states.get),
            services=SimpleNamespace(async_call=AsyncMock()),
            async_create_task=Mock(side_effect=lambda coro: coro.close()),
        )
        self.device.async_write_ha_state = Mock()
        self.call = self.device.hass.services.async_call

    async def command(self, current="off", desired="cool"):
        await self.device._async_command(
            "hvac_mode", current, desired, "set_hvac_mode", "hvac_mode"
        )

    async def test_lost_start_retries_without_virtual_toggle(self):
        await self.device._async_control()
        self.assertEqual(self.device._active_mode, HVACMode.COOL)
        self.assertEqual(self.device._attr_hvac_action, HVACAction.IDLE)
        self.assertIn(
            "requested; device still off", self.device._compute_short_status()[0]
        )
        original = self.call.call_count
        for _ in range(20):
            await self.device._async_control()
        self.assertEqual(self.call.call_count, original)
        self.clock += 30
        await self.device._async_control()
        modes = [c for c in self.call.call_args_list if c.args[1] == "set_hvac_mode"]
        self.assertEqual(len(modes), 2)
        self.states["climate.split"].state = "cool"
        await self.device._async_control()
        self.assertNotIn("hvac_mode", self.device._last_sent)
        self.assertEqual(self.device._attr_hvac_action, HVACAction.COOLING)

    async def test_backoff_caps_at_five_minutes(self):
        await self.command()
        for delay in (30, 60, 120, 240, 300, 300):
            before = self.call.call_count
            self.clock += delay - 1
            await self.command()
            self.assertEqual(self.call.call_count, before)
            self.clock += 1
            await self.command()
            self.assertEqual(self.call.call_count, before + 1)

    async def test_confirmation_then_external_off_reasserts_immediately(self):
        await self.command()
        await self.command(current="cool")
        await self.command()
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.device._command_attempts["hvac_mode"][1], 1)

    async def test_brief_disconnect_retries_even_if_recovered_before_task_runs(self):
        await self.device._async_control()
        self.call.reset_mock()
        self.device._async_state_changed(
            SimpleNamespace(
                data={"entity_id": "climate.split", "new_state": state("unavailable")}
            )
        )
        await self.device._async_control()
        self.assertTrue(
            any(c.args[1] == "set_hvac_mode" for c in self.call.call_args_list)
        )
        self.assertFalse(self.device._downstream_disconnected)

    async def test_service_exception_uses_same_backoff(self):
        self.call.side_effect = RuntimeError("connection lost")
        with self.assertLogs("delivery_test_climate", logging.ERROR):
            await self.command()
        self.call.side_effect = None
        await self.command()
        self.assertEqual(self.call.call_count, 1)
        self.clock += 30
        await self.command()
        self.assertEqual(self.call.call_count, 2)

    async def test_off_supersedes_pending_cool_and_retries(self):
        await self.command()
        self.states["climate.split"].state = "cool"
        await self.device.async_set_hvac_mode(HVACMode.OFF)
        self.assertEqual(self.device._last_sent["hvac_mode"], "off")
        before = self.call.call_count
        await self.device._async_control()
        self.assertEqual(self.call.call_count, before)
        self.clock += 30
        await self.device._async_control()
        self.assertEqual(self.call.call_count, before + 1)
        self.assertEqual(self.call.call_args.args[2]["hvac_mode"], "off")

    async def test_off_while_disconnected_is_reasserted_on_recovery(self):
        self.states["climate.split"].state = "unavailable"
        await self.device.async_set_hvac_mode(HVACMode.OFF)
        self.call.assert_not_called()
        self.states["climate.split"].state = "cool"
        await self.device._async_control()
        self.assertEqual(self.call.call_args.args[2]["hvac_mode"], "off")

    async def test_setpoint_and_fan_retries_are_independent(self):
        await self.device._async_send(
            self.states["climate.split"], HVACMode.COOL, 68, "high"
        )
        self.assertEqual(self.call.call_count, 3)
        self.states["climate.split"].state = "cool"
        self.states["climate.split"].attributes["fan_mode"] = "high"
        await self.device._async_send(
            self.states["climate.split"], HVACMode.COOL, 68, "high"
        )
        self.clock += 30
        await self.device._async_send(
            self.states["climate.split"], HVACMode.COOL, 68, "high"
        )
        self.assertEqual(self.call.call_count, 4)
        self.assertEqual(self.call.call_args.args[1], "set_temperature")

    async def test_learning_breaks_across_unconfirmed_operation(self):
        self.device._thermal_learner = Mock()
        await self.device._async_control()
        self.device._thermal_learner.reset_mock()
        self.device._thermal_learner.project.return_value = (76.0, 0.0)
        await self.device._async_control()
        self.device._thermal_learner.observe.assert_not_called()
        self.device._thermal_learner.reset_observation.assert_called_once()

    async def test_emergency_standby_retries_failed_stop(self):
        self.states["sensor.room"].state = "unavailable"
        self.states["climate.split"].state = "cool"
        await self.device._async_control()
        self.assertEqual(self.call.call_count, 1)
        self.clock += 30
        await self.device._async_control()
        self.assertEqual(self.call.call_count, 2)
        self.assertEqual(self.call.call_args.args[2]["hvac_mode"], "off")

    async def test_hung_service_releases_control_for_retry(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()

        self.call.side_effect = hang
        with patch.dict(NS, COMMAND_TIMEOUT_S=0.001):
            with self.assertLogs("delivery_test_climate", logging.ERROR):
                await self.command()
        self.call.side_effect = None
        self.clock += 30
        await self.command()
        self.assertEqual(self.call.call_count, 2)

    async def test_missing_device_does_not_claim_cooling(self):
        await self.device._async_control()
        self.states["climate.split"].state = "unavailable"
        await self.device._async_control()
        self.assertEqual(self.device._attr_hvac_action, HVACAction.IDLE)
        self.assertEqual(
            self.device._compute_short_status()[0], "Waiting for downstream device"
        )

    async def test_reconnect_does_not_bypass_minimum_cycle_hold(self):
        self.device._last_transition = datetime.now(timezone.utc) - timedelta(
            seconds=10
        )
        self.device._downstream_disconnected = True
        await self.device._async_control()
        self.call.assert_not_called()
        self.assertIsNone(self.device._active_mode)
        self.assertIn("Min cycle hold", self.device._decision_reason)

    async def test_mode_setter_waits_for_inflight_control(self):
        await self.device._control_lock.acquire()
        task = asyncio.create_task(self.device.async_set_hvac_mode(HVACMode.OFF))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertNotEqual(self.device._attr_hvac_mode, HVACMode.OFF)
        self.device._control_lock.release()
        await task
        self.assertEqual(self.device._attr_hvac_mode, HVACMode.OFF)


if __name__ == "__main__":
    unittest.main()
