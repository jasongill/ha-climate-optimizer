"""Virtual climate entity that drives a downstream climate device."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import (
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AREA_ID,
    CONF_COOL_TARGET,
    CONF_DEADBAND,
    CONF_DOWNSTREAM_CLIMATE,
    CONF_EMERGENCY_COOL_ABOVE_OUTDOOR,
    CONF_EMERGENCY_COOL_SETPOINT,
    CONF_EMERGENCY_ENABLE,
    CONF_EMERGENCY_FAN_MODE,
    CONF_EMERGENCY_HEAT_BELOW_OUTDOOR,
    CONF_EMERGENCY_HEAT_SETPOINT,
    CONF_HEAT_TARGET,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_SETPOINT_OFFSET,
    CONF_SOURCE_HUMIDITY_SENSOR,
    CONF_SOURCE_TEMP_SENSOR,
    CONF_TICK_INTERVAL,
    DOMAIN,
    FAN_TIER_KEYS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the virtual climate entity for a config entry."""
    merged = {**entry.data, **entry.options}
    async_add_entities([VirtualClimateDevice(entry, merged)])


def _as_float(state: State | None) -> float | None:
    if state is None or state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _build_fan_tiers(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the flat tier fields from config, sorted by ascending max_error."""
    tiers: list[dict[str, Any]] = []
    for err_key, err_default, mode_key, mode_default in FAN_TIER_KEYS:
        mode = str(cfg.get(mode_key, mode_default)).strip()
        if not mode:
            continue
        tiers.append(
            {
                "max_error": float(cfg.get(err_key, err_default)),
                "fan_mode": mode,
            }
        )
    tiers.sort(key=lambda t: t["max_error"])
    return tiers


class VirtualClimateDevice(ClimateEntity):
    """Virtual climate entity that drives a downstream unit from a room sensor."""

    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT_COOL,
        HVACMode.HEAT,
        HVACMode.COOL,
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, cfg: dict[str, Any]) -> None:
        self._entry_id = entry.entry_id

        self._attr_name = cfg[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_virtual_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=cfg[CONF_NAME],
            manufacturer="Climate Optimizer",
            model="Virtual Climate Device",
        )
        self._area_id: str | None = cfg.get(CONF_AREA_ID)

        self._source_temp: str = cfg[CONF_SOURCE_TEMP_SENSOR]
        self._source_humidity: str | None = cfg.get(CONF_SOURCE_HUMIDITY_SENSOR)
        self._downstream: str = cfg[CONF_DOWNSTREAM_CLIMATE]
        self._outdoor_sensor: str | None = cfg.get(CONF_OUTDOOR_TEMP_SENSOR)

        self._heat_target = float(cfg[CONF_HEAT_TARGET])
        self._cool_target = float(cfg[CONF_COOL_TARGET])
        self._deadband = float(cfg[CONF_DEADBAND])
        self._offset = float(cfg[CONF_SETPOINT_OFFSET])
        self._min_cycle = int(cfg[CONF_MIN_CYCLE_TIME])
        self._tick_interval = int(cfg[CONF_TICK_INTERVAL])
        self._fan_tiers = _build_fan_tiers(cfg)

        self._emergency_enable = bool(cfg[CONF_EMERGENCY_ENABLE])
        self._emergency_heat_below = float(cfg[CONF_EMERGENCY_HEAT_BELOW_OUTDOOR])
        self._emergency_cool_above = float(cfg[CONF_EMERGENCY_COOL_ABOVE_OUTDOOR])
        self._emergency_heat_setpoint = float(cfg[CONF_EMERGENCY_HEAT_SETPOINT])
        self._emergency_cool_setpoint = float(cfg[CONF_EMERGENCY_COOL_SETPOINT])
        self._emergency_fan_mode = str(cfg[CONF_EMERGENCY_FAN_MODE])

        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self._attr_hvac_action = HVACAction.IDLE
        self._attr_fan_mode: str | None = None

        self._active_mode: HVACMode | None = None
        self._emergency_active = False
        self._last_transition: datetime | None = None
        self._last_sent: dict[str, Any] = {}

        self._decision_reason = "Starting up"
        self._last_room_temp: float | None = None
        self._last_error: float | None = None
        self._last_pushed_setpoint: float | None = None
        self._last_fan_tier: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        tracked = [self._source_temp, self._downstream]
        if self._source_humidity:
            tracked.append(self._source_humidity)
        if self._outdoor_sensor:
            tracked.append(self._outdoor_sensor)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, tracked, self._async_state_changed
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_tick,
                timedelta(seconds=self._tick_interval),
            )
        )

        if self._area_id:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, self._entry_id)}
            )
            if device is not None and device.area_id != self._area_id:
                dev_reg.async_update_device(device.id, area_id=self._area_id)

        self.hass.async_create_task(self._async_control())

    # ------------------------------------------------------------------ properties

    @property
    def current_temperature(self) -> float | None:
        return _as_float(self.hass.states.get(self._source_temp))

    @property
    def current_humidity(self) -> float | None:
        if not self._source_humidity:
            return None
        return _as_float(self.hass.states.get(self._source_humidity))

    @property
    def target_temperature_low(self) -> float:
        return self._heat_target

    @property
    def target_temperature_high(self) -> float:
        return self._cool_target

    @property
    def min_temp(self) -> float:
        return self._downstream_limits()[0]

    @property
    def max_temp(self) -> float:
        return self._downstream_limits()[1]

    @property
    def fan_modes(self) -> list[str] | None:
        ds = self.hass.states.get(self._downstream)
        if ds is None:
            return None
        return ds.attributes.get(ATTR_FAN_MODES)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "decision_reason": self._decision_reason,
            "active_mode": self._active_mode.value if self._active_mode else None,
            "room_temperature": self._last_room_temp,
            "heat_target": self._heat_target,
            "cool_target": self._cool_target,
            "deadband": self._deadband,
            "error_from_band": self._last_error,
            "pushed_setpoint": self._last_pushed_setpoint,
            "active_fan_tier": self._last_fan_tier,
            "setpoint_offset": self._offset,
            "fan_tiers": self._fan_tiers,
            "source_temp_sensor": self._source_temp,
            "source_humidity_sensor": self._source_humidity,
            "downstream_climate": self._downstream,
            "outdoor_temp_sensor": self._outdoor_sensor,
            "emergency_enabled": self._emergency_enable,
            "emergency_active": self._emergency_active,
            "last_transition": self._last_transition.isoformat()
            if self._last_transition
            else None,
            "last_sent": self._last_sent,
        }

    # ------------------------------------------------------------------ user commands

    async def async_set_temperature(self, **kwargs: Any) -> None:
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        single = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        if hvac_mode is not None:
            self._attr_hvac_mode = hvac_mode
        if low is not None:
            self._heat_target = float(low)
        if high is not None:
            self._cool_target = float(high)
        if single is not None and low is None and high is None:
            mid = float(single)
            half = (self._cool_target - self._heat_target) / 2 or 2.5
            self._heat_target = mid - half
            self._cool_target = mid + half

        if self._heat_target >= self._cool_target:
            _LOGGER.warning(
                "Invalid target range (heat %.1f >= cool %.1f), ignoring",
                self._heat_target,
                self._cool_target,
            )
            return

        self.async_write_ha_state()
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            await self._async_stop_downstream()
            self._active_mode = None
            self._attr_hvac_action = HVACAction.OFF
        elif (
            (self._active_mode == HVACMode.COOL and hvac_mode == HVACMode.HEAT)
            or (self._active_mode == HVACMode.HEAT and hvac_mode == HVACMode.COOL)
        ):
            await self._async_stop_downstream()
            self._active_mode = None
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_fan_mode",
            {"entity_id": self._downstream, "fan_mode": fan_mode},
            blocking=False,
        )
        self._attr_fan_mode = fan_mode
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL)

    # ------------------------------------------------------------------ event hooks

    @callback
    def _async_state_changed(self, event: Event) -> None:
        self.hass.async_create_task(self._async_control())

    async def _async_tick(self, _now: datetime) -> None:
        await self._async_control()

    # ------------------------------------------------------------------ control core

    async def _async_control(self) -> None:
        if self._attr_hvac_mode == HVACMode.OFF:
            self._decision_reason = "Virtual device is OFF"
            self.async_write_ha_state()
            return

        ds_state = self.hass.states.get(self._downstream)
        if ds_state is None or ds_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._decision_reason = (
                f"Downstream climate {self._downstream} unavailable; holding"
            )
            _LOGGER.warning(
                "Downstream climate %s unavailable; skipping tick", self._downstream
            )
            self.async_write_ha_state()
            return

        allow_heat = self._attr_hvac_mode in (HVACMode.HEAT_COOL, HVACMode.HEAT)
        allow_cool = self._attr_hvac_mode in (HVACMode.HEAT_COOL, HVACMode.COOL)

        room_temp = self.current_temperature
        self._last_room_temp = room_temp
        if room_temp is None:
            await self._async_handle_room_sensor_lost(ds_state, allow_heat, allow_cool)
            return

        self._emergency_active = False

        desired: HVACMode | None = self._active_mode
        transition_reason: str | None = None

        if desired is None:
            # Idle: decide whether to start a cycle.
            if allow_cool and room_temp > self._cool_target + self._deadband:
                desired = HVACMode.COOL
                transition_reason = (
                    f"Starting COOL: room {room_temp:.1f}°F > cool_target + deadband "
                    f"({self._cool_target:.1f} + {self._deadband:.1f} = "
                    f"{self._cool_target + self._deadband:.1f}°F)"
                )
            elif allow_heat and room_temp < self._heat_target - self._deadband:
                desired = HVACMode.HEAT
                transition_reason = (
                    f"Starting HEAT: room {room_temp:.1f}°F < heat_target − deadband "
                    f"({self._heat_target:.1f} − {self._deadband:.1f} = "
                    f"{self._heat_target - self._deadband:.1f}°F)"
                )
            else:
                self._decision_reason = (
                    f"IDLE: room {room_temp:.1f}°F is inside target band "
                    f"{self._heat_target:.1f}–{self._cool_target:.1f}°F "
                    f"(start thresholds <{self._heat_target - self._deadband:.1f} "
                    f"or >{self._cool_target + self._deadband:.1f})"
                )
        else:
            # Running: stop when we reach the target edge. No deadband on the
            # stop side — we stop AT the target and then min_cycle_time
            # prevents immediate restart, which avoids short-cycling.
            if desired == HVACMode.COOL and room_temp <= self._cool_target:
                desired = None
                transition_reason = (
                    f"Ending COOL: room {room_temp:.1f}°F reached cool_target "
                    f"{self._cool_target:.1f}°F"
                )
            elif desired == HVACMode.HEAT and room_temp >= self._heat_target:
                desired = None
                transition_reason = (
                    f"Ending HEAT: room {room_temp:.1f}°F reached heat_target "
                    f"{self._heat_target:.1f}°F"
                )

        # Minimum cycle time gate on transitions.
        if desired != self._active_mode and self._last_transition is not None:
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                remaining = int(self._min_cycle - elapsed)
                transition_reason = (
                    f"Min cycle hold: wanted to change to "
                    f"{desired.value if desired else 'idle'} but {remaining}s "
                    f"remain of min_cycle_time ({self._min_cycle}s)"
                )
                desired = self._active_mode

        if desired != self._active_mode:
            self._active_mode = desired
            self._last_transition = dt_util.utcnow()
            if desired is None:
                await self._async_stop_downstream()
                self._attr_hvac_action = HVACAction.IDLE
                self._last_error = 0.0
                self._last_pushed_setpoint = None
                self._last_fan_tier = None
                self._decision_reason = (
                    f"{transition_reason or 'Stopped'}. "
                    f"Will stay off for at least {self._min_cycle}s."
                )
                self.async_write_ha_state()
                return

        if self._active_mode is None:
            self._attr_hvac_action = HVACAction.IDLE
            self._last_error = 0.0
            self._last_pushed_setpoint = None
            self._last_fan_tier = None
            if ds_state.state != "off":
                _LOGGER.warning(
                    "Downstream %s is %s while virtual device is idle; "
                    "re-asserting off",
                    self._downstream,
                    ds_state.state,
                )
                self._last_sent = {}
                await self._async_stop_downstream()
                self._decision_reason = (
                    f"IDLE: downstream was {ds_state.state}, re-asserted off. "
                    f"{self._decision_reason}"
                )
            self.async_write_ha_state()
            return

        reason = await self._async_drive_active(room_temp, self._active_mode, ds_state)
        self._decision_reason = (
            f"{transition_reason}. {reason}" if transition_reason else reason
        )
        self._attr_hvac_action = (
            HVACAction.COOLING
            if self._active_mode == HVACMode.COOL
            else HVACAction.HEATING
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ room sensor lost

    async def _async_handle_room_sensor_lost(
        self, ds_state: State, allow_heat: bool, allow_cool: bool
    ) -> None:
        """Emergency fallback when the room sensor is unavailable."""
        was_emergency = self._emergency_active

        if not self._emergency_enable:
            if not was_emergency and self._active_mode is not None:
                _LOGGER.warning(
                    "Source temp sensor %s unavailable and emergency mode "
                    "disabled; turning downstream off",
                    self._source_temp,
                )
            await self._async_go_idle()
            self._decision_reason = (
                f"Room sensor {self._source_temp} unavailable and emergency "
                "mode is disabled; downstream turned off for safety."
            )
            self.async_write_ha_state()
            return

        outdoor_temp: float | None = None
        if self._outdoor_sensor:
            outdoor_temp = _as_float(self.hass.states.get(self._outdoor_sensor))

        desired: HVACMode | None = None
        if outdoor_temp is not None:
            if allow_heat and outdoor_temp < self._emergency_heat_below:
                desired = HVACMode.HEAT
            elif allow_cool and outdoor_temp > self._emergency_cool_above:
                desired = HVACMode.COOL

        if desired is None:
            if not was_emergency:
                _LOGGER.warning(
                    "Source temp sensor %s unavailable; emergency conditions "
                    "not met (outdoor=%s), turning downstream off",
                    self._source_temp,
                    outdoor_temp,
                )
            await self._async_go_idle()
            outdoor_str = (
                f"{outdoor_temp:.1f}°F" if outdoor_temp is not None else "unavailable"
            )
            self._decision_reason = (
                f"EMERGENCY STANDBY: room sensor {self._source_temp} "
                f"unavailable, outdoor {outdoor_str}. Within safe band "
                f"({self._emergency_heat_below:.0f}–"
                f"{self._emergency_cool_above:.0f}°F), downstream off."
            )
            self.async_write_ha_state()
            return

        # Apply min-cycle gate to emergency transitions too.
        if desired != self._active_mode and self._last_transition is not None:
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                desired = self._active_mode or desired

        if desired != self._active_mode:
            self._active_mode = desired
            self._last_transition = dt_util.utcnow()

        self._emergency_active = True
        if not was_emergency:
            _LOGGER.warning(
                "EMERGENCY mode active: room sensor %s unavailable, "
                "outdoor=%.1f, driving downstream in %s",
                self._source_temp,
                outdoor_temp if outdoor_temp is not None else float("nan"),
                desired,
            )

        setpoint = (
            self._emergency_cool_setpoint
            if desired == HVACMode.COOL
            else self._emergency_heat_setpoint
        )
        await self._async_send(ds_state, desired, setpoint, self._emergency_fan_mode)

        self._last_error = None
        self._last_pushed_setpoint = setpoint
        self._last_fan_tier = self._emergency_fan_mode

        cmp_str = "<" if desired == HVACMode.HEAT else ">"
        thresh = (
            self._emergency_heat_below
            if desired == HVACMode.HEAT
            else self._emergency_cool_above
        )
        self._decision_reason = (
            f"EMERGENCY {desired.value.upper()}: room sensor "
            f"{self._source_temp} unavailable, outdoor {outdoor_temp:.1f}°F "
            f"{cmp_str} threshold {thresh:.0f}°F. "
            "Driving downstream at fixed emergency setpoint."
        )

        self._attr_hvac_action = (
            HVACAction.COOLING if desired == HVACMode.COOL else HVACAction.HEATING
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ downstream drive

    async def _async_drive_active(
        self, room_temp: float, mode: HVACMode, ds_state: State
    ) -> str:
        """Compute and send a normal (sensor-driven) downstream command."""
        ds_min, ds_max, ds_step = self._downstream_limits(ds_state)

        if mode == HVACMode.COOL:
            raw_setpoint = self._cool_target - self._offset
            error = max(0.0, room_temp - self._cool_target)
        else:
            raw_setpoint = self._heat_target + self._offset
            error = max(0.0, self._heat_target - room_temp)

        setpoint = self._clamp(raw_setpoint, ds_min, ds_max, ds_step)
        available_fan = ds_state.attributes.get(ATTR_FAN_MODES) or []
        fan_mode = self._pick_fan_mode(error, available_fan)

        self._last_error = error
        self._last_pushed_setpoint = setpoint
        self._last_fan_tier = fan_mode

        await self._async_send(ds_state, mode, setpoint, fan_mode)

        if mode == HVACMode.COOL:
            target_label = f"cool_target {self._cool_target:.1f}°F"
            stop_label = f"will stop at {self._cool_target:.1f}°F"
            offset_sign = "−"
        else:
            target_label = f"heat_target {self._heat_target:.1f}°F"
            stop_label = f"will stop at {self._heat_target:.1f}°F"
            offset_sign = "+"

        return (
            f"{mode.value.upper()}ING: room {room_temp:.1f}°F, {target_label}, "
            f"error {error:.1f}°F. Pushing downstream setpoint to "
            f"{setpoint:.0f}°F (target {offset_sign} {self._offset:.0f}°F offset, "
            f"clamped to {ds_min:.0f}–{ds_max:.0f}). "
            f"Fan tier: {fan_mode or 'n/a'}. {stop_label}."
        )

    async def _async_send(
        self,
        ds_state: State,
        mode: HVACMode,
        setpoint: float,
        fan_mode: str | None,
    ) -> None:
        """Send hvac_mode/setpoint/fan_mode to the downstream, deduped."""
        desired_hvac = mode.value  # "heat" / "cool"

        cur_mode = ds_state.state
        cur_setpoint = ds_state.attributes.get(ATTR_TEMPERATURE)
        cur_fan = ds_state.attributes.get(ATTR_FAN_MODE)

        if cur_mode != desired_hvac and self._last_sent.get("hvac_mode") != desired_hvac:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._downstream, "hvac_mode": desired_hvac},
                blocking=True,
            )
            self._last_sent["hvac_mode"] = desired_hvac

        if cur_setpoint != setpoint and self._last_sent.get("setpoint") != setpoint:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._downstream, "temperature": setpoint},
                blocking=True,
            )
            self._last_sent["setpoint"] = setpoint

        available_fan = ds_state.attributes.get(ATTR_FAN_MODES) or []
        if fan_mode and fan_mode not in available_fan:
            fan_mode = available_fan[0] if available_fan else None

        if fan_mode and cur_fan != fan_mode and self._last_sent.get("fan_mode") != fan_mode:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": self._downstream, "fan_mode": fan_mode},
                blocking=True,
            )
            self._last_sent["fan_mode"] = fan_mode
            self._attr_fan_mode = fan_mode

    async def _async_go_idle(self) -> None:
        """Stop downstream and clear active state."""
        if self._active_mode is not None or self._emergency_active:
            await self._async_stop_downstream()
            self._active_mode = None
            self._emergency_active = False
            self._last_transition = dt_util.utcnow()
        self._attr_hvac_action = HVACAction.IDLE
        self._last_error = 0.0
        self._last_pushed_setpoint = None
        self._last_fan_tier = None

    async def _async_stop_downstream(self) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._downstream, "hvac_mode": "off"},
            blocking=True,
        )
        self._last_sent = {"hvac_mode": "off"}

    # ------------------------------------------------------------------ helpers

    def _pick_fan_mode(self, error: float, available: list[str]) -> str | None:
        if not available:
            return None
        for tier in self._fan_tiers:
            if error <= tier["max_error"] and tier["fan_mode"] in available:
                return tier["fan_mode"]
        for tier in reversed(self._fan_tiers):
            if tier["fan_mode"] in available:
                return tier["fan_mode"]
        return available[0]

    def _downstream_limits(
        self, ds_state: State | None = None
    ) -> tuple[float, float, float]:
        """Return (min_temp, max_temp, step) from the downstream, with fallbacks."""
        if ds_state is None:
            ds_state = self.hass.states.get(self._downstream)
        default = (45.0, 95.0, 1.0)
        if ds_state is None:
            return default
        try:
            return (
                float(ds_state.attributes.get(ATTR_MIN_TEMP, default[0])),
                float(ds_state.attributes.get(ATTR_MAX_TEMP, default[1])),
                float(ds_state.attributes.get(ATTR_TARGET_TEMP_STEP, default[2])),
            )
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp(value: float, low: float, high: float, step: float) -> float:
        value = max(low, min(high, value))
        if step > 0:
            value = round(value / step) * step
        return value
