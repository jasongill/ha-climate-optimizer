"""Virtual zone climate entity that drives a downstream climate device."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COOL_TARGET,
    CONF_DEADBAND,
    CONF_DOWNSTREAM_CLIMATE,
    CONF_EMERGENCY_COOL_ABOVE_OUTDOOR,
    CONF_EMERGENCY_COOL_SETPOINT,
    CONF_EMERGENCY_ENABLE,
    CONF_EMERGENCY_FAN_MODE,
    CONF_EMERGENCY_HEAT_BELOW_OUTDOOR,
    CONF_EMERGENCY_HEAT_SETPOINT,
    CONF_FAN_TIERS,
    CONF_HEAT_TARGET,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_SETPOINT_OFFSET,
    CONF_SOURCE_HUMIDITY_SENSOR,
    CONF_SOURCE_TEMP_SENSOR,
    CONF_TICK_INTERVAL,
    DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
    DEFAULT_EMERGENCY_COOL_SETPOINT,
    DEFAULT_EMERGENCY_ENABLE,
    DEFAULT_EMERGENCY_FAN_MODE,
    DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
    DEFAULT_EMERGENCY_HEAT_SETPOINT,
    DEFAULT_FAN_TIERS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    merged = {**entry.data, **entry.options}
    async_add_entities([VirtualZoneClimate(hass, entry, merged)])


def _as_float(state: State | None) -> float | None:
    if state is None or state.state in (None, "", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


class VirtualZoneClimate(ClimateEntity):
    """Virtual climate entity that controls a downstream minisplit from a room sensor."""

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

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, cfg: dict[str, Any]
    ) -> None:
        self.hass = hass
        self._entry = entry

        self._attr_name = cfg[CONF_NAME]
        self._attr_unique_id = f"{entry.entry_id}_zone_climate"

        self._source_temp: str = cfg[CONF_SOURCE_TEMP_SENSOR]
        self._source_humidity: str | None = cfg.get(CONF_SOURCE_HUMIDITY_SENSOR)
        self._downstream: str = cfg[CONF_DOWNSTREAM_CLIMATE]

        self._heat_target = float(cfg[CONF_HEAT_TARGET])
        self._cool_target = float(cfg[CONF_COOL_TARGET])
        self._deadband = float(cfg[CONF_DEADBAND])
        self._offset = float(cfg[CONF_SETPOINT_OFFSET])
        self._min_cycle = int(cfg[CONF_MIN_CYCLE_TIME])
        self._tick_interval = int(cfg[CONF_TICK_INTERVAL])
        self._fan_tiers: list[dict[str, Any]] = (
            cfg.get(CONF_FAN_TIERS) or DEFAULT_FAN_TIERS
        )

        self._outdoor_sensor: str | None = cfg.get(CONF_OUTDOOR_TEMP_SENSOR)
        self._emergency_enable: bool = bool(
            cfg.get(CONF_EMERGENCY_ENABLE, DEFAULT_EMERGENCY_ENABLE)
        )
        self._emergency_heat_below: float = float(
            cfg.get(
                CONF_EMERGENCY_HEAT_BELOW_OUTDOOR,
                DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
            )
        )
        self._emergency_cool_above: float = float(
            cfg.get(
                CONF_EMERGENCY_COOL_ABOVE_OUTDOOR,
                DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
            )
        )
        self._emergency_heat_setpoint: float = float(
            cfg.get(CONF_EMERGENCY_HEAT_SETPOINT, DEFAULT_EMERGENCY_HEAT_SETPOINT)
        )
        self._emergency_cool_setpoint: float = float(
            cfg.get(CONF_EMERGENCY_COOL_SETPOINT, DEFAULT_EMERGENCY_COOL_SETPOINT)
        )
        self._emergency_fan_mode: str = str(
            cfg.get(CONF_EMERGENCY_FAN_MODE, DEFAULT_EMERGENCY_FAN_MODE)
        )

        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self._attr_hvac_action = HVACAction.IDLE
        self._attr_fan_mode: str | None = None

        self._active_mode: HVACMode | None = None  # HEAT / COOL while running
        self._emergency_active: bool = False
        self._last_transition: datetime | None = None
        self._last_sent: dict[str, Any] = {}

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
        ds = self.hass.states.get(self._downstream)
        if ds is not None:
            try:
                return float(ds.attributes.get("min_temp", 45))
            except (TypeError, ValueError):
                pass
        return 45.0

    @property
    def max_temp(self) -> float:
        ds = self.hass.states.get(self._downstream)
        if ds is not None:
            try:
                return float(ds.attributes.get("max_temp", 95))
            except (TypeError, ValueError):
                pass
        return 95.0

    @property
    def fan_modes(self) -> list[str] | None:
        ds = self.hass.states.get(self._downstream)
        if ds is None:
            return None
        return ds.attributes.get("fan_modes")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "active_mode": self._active_mode.value if self._active_mode else None,
            "setpoint_offset": self._offset,
            "deadband": self._deadband,
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
        else:
            # If the currently-running mode is no longer permitted, stop it.
            if (
                self._active_mode == HVACMode.COOL and hvac_mode == HVACMode.HEAT
            ) or (
                self._active_mode == HVACMode.HEAT and hvac_mode == HVACMode.COOL
            ):
                await self._async_stop_downstream()
                self._active_mode = None
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        # Manual fan override; forwards straight through to downstream.
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
            return

        ds_state = self.hass.states.get(self._downstream)
        if ds_state is None or ds_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "Downstream climate %s unavailable; skipping tick", self._downstream
            )
            return

        allow_heat = self._attr_hvac_mode in (HVACMode.HEAT_COOL, HVACMode.HEAT)
        allow_cool = self._attr_hvac_mode in (HVACMode.HEAT_COOL, HVACMode.COOL)

        room_temp = self.current_temperature
        if room_temp is None:
            await self._async_handle_room_sensor_lost(ds_state, allow_heat, allow_cool)
            return

        # Room sensor is healthy again — clear the emergency flag if it was set.
        self._emergency_active = False

        desired: HVACMode | None = self._active_mode

        if desired is None:
            # Idle: decide whether to start a cycle.
            if allow_cool and room_temp > self._cool_target + self._deadband:
                desired = HVACMode.COOL
            elif allow_heat and room_temp < self._heat_target - self._deadband:
                desired = HVACMode.HEAT
        else:
            # Running: stop when we reach the target edge (no deadband here — we
            # want to stop *at* the target, then wait min_cycle before restarting).
            if desired == HVACMode.COOL and room_temp <= self._cool_target:
                desired = None
            elif desired == HVACMode.HEAT and room_temp >= self._heat_target:
                desired = None

        # Minimum cycle time gate on transitions.
        if desired != self._active_mode and self._last_transition is not None:
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                _LOGGER.debug(
                    "Min cycle time not elapsed (%.0fs < %ds), holding mode %s",
                    elapsed,
                    self._min_cycle,
                    self._active_mode,
                )
                desired = self._active_mode

        if desired != self._active_mode:
            self._active_mode = desired
            self._last_transition = dt_util.utcnow()
            if desired is None:
                await self._async_stop_downstream()
                self._attr_hvac_action = HVACAction.IDLE
                self.async_write_ha_state()
                return

        if self._active_mode is None:
            self._attr_hvac_action = HVACAction.IDLE
            self.async_write_ha_state()
            return

        await self._async_drive_downstream(room_temp, self._active_mode, ds_state)
        self._attr_hvac_action = (
            HVACAction.COOLING
            if self._active_mode == HVACMode.COOL
            else HVACAction.HEATING
        )
        self.async_write_ha_state()

    async def _async_handle_room_sensor_lost(
        self, ds_state: State, allow_heat: bool, allow_cool: bool
    ) -> None:
        """Decide what to do when the room sensor is unavailable.

        Behavior:
          - If emergency mode is disabled, turn the downstream off (safe default)
            rather than leaving it running blind.
          - If emergency mode is enabled and an outdoor sensor is configured and
            readable, drive heat/cool conservatively based on outdoor thresholds.
          - Otherwise (no outdoor sensor or it's also dead), turn off.
        """
        if not self._emergency_enable:
            _LOGGER.warning(
                "Source temp sensor %s unavailable and emergency mode disabled; "
                "turning downstream off",
                self._source_temp,
            )
            if self._active_mode is not None or self._emergency_active:
                await self._async_stop_downstream()
                self._active_mode = None
                self._emergency_active = False
                self._last_transition = dt_util.utcnow()
            self._attr_hvac_action = HVACAction.IDLE
            self.async_write_ha_state()
            return

        outdoor_temp: float | None = None
        if self._outdoor_sensor:
            outdoor_temp = _as_float(self.hass.states.get(self._outdoor_sensor))

        desired_mode: HVACMode | None = None
        if outdoor_temp is not None:
            if allow_heat and outdoor_temp < self._emergency_heat_below:
                desired_mode = HVACMode.HEAT
            elif allow_cool and outdoor_temp > self._emergency_cool_above:
                desired_mode = HVACMode.COOL

        if desired_mode is None:
            # Nothing to do — safe default is off.
            _LOGGER.warning(
                "Source temp sensor %s unavailable; emergency conditions not met "
                "(outdoor=%s), turning downstream off",
                self._source_temp,
                outdoor_temp,
            )
            if self._active_mode is not None or self._emergency_active:
                await self._async_stop_downstream()
                self._active_mode = None
                self._emergency_active = False
                self._last_transition = dt_util.utcnow()
            self._attr_hvac_action = HVACAction.IDLE
            self.async_write_ha_state()
            return

        # Apply min-cycle gate to emergency transitions too.
        if desired_mode != self._active_mode and self._last_transition is not None:
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                _LOGGER.debug(
                    "Emergency: min cycle not elapsed (%.0fs < %ds), holding",
                    elapsed,
                    self._min_cycle,
                )
                desired_mode = self._active_mode or desired_mode

        if desired_mode != self._active_mode:
            self._active_mode = desired_mode
            self._last_transition = dt_util.utcnow()

        self._emergency_active = True
        _LOGGER.warning(
            "EMERGENCY mode active: room sensor %s unavailable, outdoor=%.1f, "
            "driving downstream in %s",
            self._source_temp,
            outdoor_temp if outdoor_temp is not None else float("nan"),
            desired_mode,
        )

        await self._async_drive_emergency(desired_mode, ds_state)
        self._attr_hvac_action = (
            HVACAction.COOLING
            if desired_mode == HVACMode.COOL
            else HVACAction.HEATING
        )
        self.async_write_ha_state()

    async def _async_drive_emergency(
        self, mode: HVACMode, ds_state: State
    ) -> None:
        """Command the downstream with fixed emergency setpoint + fan mode."""
        try:
            ds_min = float(ds_state.attributes.get("min_temp", 60))
            ds_max = float(ds_state.attributes.get("max_temp", 90))
            ds_step = float(ds_state.attributes.get("target_temp_step", 1))
        except (TypeError, ValueError):
            ds_min, ds_max, ds_step = 60.0, 90.0, 1.0

        if mode == HVACMode.COOL:
            desired_hvac = "cool"
            raw_setpoint = self._emergency_cool_setpoint
        else:
            desired_hvac = "heat"
            raw_setpoint = self._emergency_heat_setpoint

        setpoint = max(ds_min, min(ds_max, raw_setpoint))
        if ds_step > 0:
            setpoint = round(setpoint / ds_step) * ds_step

        available_fan = ds_state.attributes.get("fan_modes") or []
        fan_mode: str | None = (
            self._emergency_fan_mode
            if self._emergency_fan_mode in available_fan
            else (available_fan[0] if available_fan else None)
        )

        cur_mode = ds_state.state
        cur_setpoint = ds_state.attributes.get("temperature")
        cur_fan = ds_state.attributes.get("fan_mode")

        if cur_mode != desired_hvac and self._last_sent.get("hvac_mode") != desired_hvac:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._downstream, "hvac_mode": desired_hvac},
                blocking=True,
            )
            self._last_sent["hvac_mode"] = desired_hvac

        if (
            cur_setpoint != setpoint
            and self._last_sent.get("setpoint") != setpoint
        ):
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._downstream, "temperature": setpoint},
                blocking=True,
            )
            self._last_sent["setpoint"] = setpoint

        if fan_mode and cur_fan != fan_mode and self._last_sent.get("fan_mode") != fan_mode:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": self._downstream, "fan_mode": fan_mode},
                blocking=True,
            )
            self._last_sent["fan_mode"] = fan_mode
            self._attr_fan_mode = fan_mode

    async def _async_drive_downstream(
        self, room_temp: float, mode: HVACMode, ds_state: State
    ) -> None:
        try:
            ds_min = float(ds_state.attributes.get("min_temp", 60))
            ds_max = float(ds_state.attributes.get("max_temp", 90))
            ds_step = float(ds_state.attributes.get("target_temp_step", 1))
        except (TypeError, ValueError):
            ds_min, ds_max, ds_step = 60.0, 90.0, 1.0

        if mode == HVACMode.COOL:
            raw_setpoint = self._cool_target - self._offset
            error = max(0.0, room_temp - self._cool_target)
            desired_hvac = "cool"
        else:
            raw_setpoint = self._heat_target + self._offset
            error = max(0.0, self._heat_target - room_temp)
            desired_hvac = "heat"

        setpoint = max(ds_min, min(ds_max, raw_setpoint))
        if ds_step > 0:
            setpoint = round(setpoint / ds_step) * ds_step

        available_fan = ds_state.attributes.get("fan_modes") or []
        fan_mode = self._pick_fan_mode(error, available_fan)

        cur_mode = ds_state.state
        cur_setpoint = ds_state.attributes.get("temperature")
        cur_fan = ds_state.attributes.get("fan_mode")

        # Only resend changes, to avoid hammering esphome / the unit.
        if cur_mode != desired_hvac and self._last_sent.get("hvac_mode") != desired_hvac:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._downstream, "hvac_mode": desired_hvac},
                blocking=True,
            )
            self._last_sent["hvac_mode"] = desired_hvac

        if (
            cur_setpoint != setpoint
            and self._last_sent.get("setpoint") != setpoint
        ):
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self._downstream, "temperature": setpoint},
                blocking=True,
            )
            self._last_sent["setpoint"] = setpoint

        if fan_mode and cur_fan != fan_mode and self._last_sent.get("fan_mode") != fan_mode:
            await self.hass.services.async_call(
                "climate",
                "set_fan_mode",
                {"entity_id": self._downstream, "fan_mode": fan_mode},
                blocking=True,
            )
            self._last_sent["fan_mode"] = fan_mode
            self._attr_fan_mode = fan_mode

    def _pick_fan_mode(self, error: float, available: list[str]) -> str | None:
        if not available:
            return None
        tiers = sorted(self._fan_tiers or [], key=lambda t: float(t["max_error"]))
        # First tier whose max_error >= current error AND whose fan_mode exists downstream.
        for tier in tiers:
            if error <= float(tier["max_error"]) and tier["fan_mode"] in available:
                return tier["fan_mode"]
        # Fall back to the highest-configured tier that the downstream actually supports.
        for tier in reversed(tiers):
            if tier["fan_mode"] in available:
                return tier["fan_mode"]
        return available[0]

    async def _async_stop_downstream(self) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._downstream, "hvac_mode": "off"},
            blocking=True,
        )
        self._last_sent = {"hvac_mode": "off"}
