"""Virtual climate entity that drives a downstream climate device."""

from __future__ import annotations

import asyncio
import logging
import math
import re
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
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
    CONF_FAN_LIMIT_MODE,
    CONF_FAN_LIMIT_UNTIL,
    CONF_HEAT_TARGET,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_ROOM_SENSOR_STALE_MINUTES,
    CONF_ROOM_SENSOR_STUCK_HOURS,
    CONF_SETTLING_TIME,
    CONF_SETPOINT_OFFSET,
    CONF_SOURCE_HUMIDITY_SENSOR,
    CONF_SOURCE_TEMP_SENSOR,
    CONF_START_MEASUREMENT_DELAY,
    CONF_TICK_INTERVAL,
    DEFAULT_COOL_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
    DEFAULT_EMERGENCY_COOL_SETPOINT,
    DEFAULT_EMERGENCY_ENABLE,
    DEFAULT_EMERGENCY_FAN_MODE,
    DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
    DEFAULT_EMERGENCY_HEAT_SETPOINT,
    DEFAULT_HEAT_TARGET,
    DEFAULT_MIN_CYCLE_TIME,
    DEFAULT_ROOM_SENSOR_STALE_MINUTES,
    DEFAULT_ROOM_SENSOR_STUCK_HOURS,
    DEFAULT_SETTLING_TIME,
    DEFAULT_SETPOINT_OFFSET,
    DEFAULT_START_MEASUREMENT_DELAY,
    DEFAULT_TICK_INTERVAL,
    DOMAIN,
    FAN_TIER_KEYS,
)
from .control import (
    ThermalLearner,
    TemperatureTracker,
    gentle_setpoint_offset,
    projected_stop,
)
from .fan_limit import fan_limit_signal

_LOGGER = logging.getLogger(__name__)

CYCLE_HISTORY = 4

# Downstream temperature bias: the minisplit's own sensor often disagrees
# with the actual room because of mounting height, discharge air, or lag.
# We retain an EMA as a diagnostic, but never use it to increase demand.
BIAS_EMA_ALPHA = 0.2
# Aux/Midea minisplits often only refresh their reported current_temperature
# on a write (mode change, setpoint change), so the value can be hours
# stale. We declare the downstream sensor STALE — and stop feeding it into
# the bias EMA — when it has been unchanged for BIAS_STALE_AFTER_S while
# the room sensor has moved by more than BIAS_STALE_ROOM_DELTA °F. The
# previously-learned EMA still drives compensation while stale; we just
# don't poison it with frozen data.
BIAS_STALE_AFTER_S = 10 * 60
BIAS_STALE_ROOM_DELTA = 1.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the virtual climate entity for a config entry."""
    merged = {**entry.data, **entry.options}
    async_add_entities([VirtualClimateDevice(entry, merged)])


def _as_float_attr(value: Any) -> float | None:
    """Coerce a HA state value or attribute to float, tolerating sentinels."""
    if value is None or value in ("", STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _as_float(state: State | None) -> float | None:
    """Coerce the .state of a HA State object to float."""
    return _as_float_attr(state.state if state is not None else None)


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


class VirtualClimateDevice(ClimateEntity, RestoreEntity):
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
        self._entry = entry
        self._entry_id = entry.entry_id
        self._control_lock = asyncio.Lock()

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

        self._heat_target = float(cfg.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET))
        self._cool_target = float(cfg.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET))
        self._deadband = float(cfg.get(CONF_DEADBAND, DEFAULT_DEADBAND))
        self._offset = float(cfg.get(CONF_SETPOINT_OFFSET, DEFAULT_SETPOINT_OFFSET))
        self._min_cycle = int(cfg.get(CONF_MIN_CYCLE_TIME, DEFAULT_MIN_CYCLE_TIME))
        self._settling_time = int(cfg.get(CONF_SETTLING_TIME, DEFAULT_SETTLING_TIME))
        self._tick_interval = int(cfg.get(CONF_TICK_INTERVAL, DEFAULT_TICK_INTERVAL))
        # Skip the stop-threshold check for this many seconds after a cycle
        # starts. The downstream unit's blower can blast hot/cold air past a
        # nearby room sensor and spike its reading 3-5°F within the first
        # 1-2 minutes, which would otherwise trip stop_at instantly and shut
        # the cycle down before the room mass actually moves. Default 120s
        # lands just past the typical sensor peak.
        self._start_measurement_delay = int(
            cfg.get(CONF_START_MEASUREMENT_DELAY, DEFAULT_START_MEASUREMENT_DELAY)
        )
        self._room_sensor_stale_s = (
            int(
                cfg.get(
                    CONF_ROOM_SENSOR_STALE_MINUTES, DEFAULT_ROOM_SENSOR_STALE_MINUTES
                )
            )
            * 60
        )
        self._room_sensor_stuck_s = (
            int(cfg.get(CONF_ROOM_SENSOR_STUCK_HOURS, DEFAULT_ROOM_SENSOR_STUCK_HOURS))
            * 3600
        )
        self._fan_tiers = _build_fan_tiers(cfg)

        self._emergency_enable = bool(
            cfg.get(CONF_EMERGENCY_ENABLE, DEFAULT_EMERGENCY_ENABLE)
        )
        self._emergency_heat_below = float(
            cfg.get(
                CONF_EMERGENCY_HEAT_BELOW_OUTDOOR, DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR
            )
        )
        self._emergency_cool_above = float(
            cfg.get(
                CONF_EMERGENCY_COOL_ABOVE_OUTDOOR, DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR
            )
        )
        self._emergency_heat_setpoint = float(
            cfg.get(CONF_EMERGENCY_HEAT_SETPOINT, DEFAULT_EMERGENCY_HEAT_SETPOINT)
        )
        self._emergency_cool_setpoint = float(
            cfg.get(CONF_EMERGENCY_COOL_SETPOINT, DEFAULT_EMERGENCY_COOL_SETPOINT)
        )
        self._emergency_fan_mode = str(
            cfg.get(CONF_EMERGENCY_FAN_MODE, DEFAULT_EMERGENCY_FAN_MODE)
        )

        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self._attr_hvac_action = HVACAction.IDLE
        self._attr_fan_mode: str | None = None

        self._active_mode: HVACMode | None = None
        self._emergency_active = False
        self._last_transition: datetime | None = None
        self._settle_until: datetime | None = None
        self._last_sent: dict[str, Any] = {}
        self._temperature_tracker = TemperatureTracker()
        self._thermal_learner = ThermalLearner()
        self._learning_confidence = 0.0
        self._filtered_room_temp: float | None = None
        self._room_temp_slope: float | None = None
        self._projected_room_temp: float | None = None

        # Recent starts are exposed for diagnostics and future replay tuning.
        self._cycle_starts: dict[HVACMode, list[datetime]] = {
            HVACMode.HEAT: [],
            HVACMode.COOL: [],
        }

        # Smoothed delta between the downstream unit's sensor and the
        # room sensor. Persists across cycles since it's a property of
        # the install, not the room dynamics.
        self._ds_bias_ema: float | None = None

        # Staleness tracking for the downstream sensor — many minisplit
        # platforms only refresh on write events, so we have to detect
        # frozen values explicitly.
        self._ds_last_value: float | None = None
        self._ds_last_change_at: datetime | None = None
        self._ds_last_change_room_temp: float | None = None
        self._ds_stale: bool = False

        self._decision_reason = "Starting up"
        self._last_room_temp: float | None = None
        self._last_error: float | None = None
        self._last_pushed_setpoint: float | None = None
        self._last_fan_tier: str | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            attrs = last_state.attributes
            low = attrs.get("target_temp_low")
            high = attrs.get("target_temp_high")
            try:
                if low is not None and high is not None:
                    low_f = float(low)
                    high_f = float(high)
                    if low_f < high_f:
                        self._heat_target = low_f
                        self._cool_target = high_f
            except (ValueError, TypeError):
                _LOGGER.debug("Could not restore target temps from %s/%s", low, high)
            if last_state.state in (
                HVACMode.OFF,
                HVACMode.HEAT_COOL,
                HVACMode.HEAT,
                HVACMode.COOL,
            ):
                self._attr_hvac_mode = HVACMode(last_state.state)
            last_transition_str = attrs.get("last_transition")
            if last_transition_str:
                parsed = dt_util.parse_datetime(last_transition_str)
                if parsed is not None:
                    self._last_transition = parsed

            # Preserve the downstream sensor diagnostic across restarts. Old
            # overshoot values are intentionally not restored: this controller
            # never banks temperature past target.
            restored_bias = _as_float_attr(attrs.get("downstream_sensor_bias"))
            if restored_bias is not None:
                self._ds_bias_ema = restored_bias
            self._thermal_learner.restore(attrs.get("thermal_learning"))

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
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                fan_limit_signal(self._entry_id),
                self._async_fan_limit_updated,
            )
        )

        if self._area_id:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(identifiers={(DOMAIN, self._entry_id)})
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
        return ds.attributes.get(ATTR_FAN_MODES) if ds is not None else None

    def _compute_short_status(self) -> tuple[str, str]:
        """Return (status_text, mdi_icon) for the companion sensor.

        Designed to fit on a single dashboard line. Order matters: most
        specific / most actionable conditions win. All inputs are already
        tracked on self, so this is a pure derivation.
        """
        if self._attr_hvac_mode == HVACMode.OFF:
            return "Off", "mdi:power"
        if self._emergency_active:
            return "Emergency (sensor lost)", "mdi:alert"

        room = self._last_room_temp
        if room is None:
            return "Waiting for room sensor", "mdi:thermometer-off"

        reason = self._decision_reason or ""
        if reason.startswith("Min cycle hold"):
            m = re.search(r"(\d+)s remain", reason)
            text = (
                f"Holding {m.group(1)}s (compressor protection)"
                if m
                else "Holding (compressor protection)"
            )
            return text, "mdi:timer-sand"

        active = self._active_mode
        if active in (HVACMode.HEAT, HVACMode.COOL):
            if active == HVACMode.HEAT:
                target = self._heat_target
                bits = [f"Heating → {target:.0f}°F"]
                icon = "mdi:fire"
            else:
                target = self._cool_target
                bits = [f"Cooling → {target:.0f}°F"]
                icon = "mdi:snowflake"
            settle_remaining = self._start_settle_remaining_s()
            if settle_remaining > 0:
                bits.append(f"(settling sensor {settle_remaining}s)")
                icon = "mdi:timer-sand-paused"
            extras: list[str] = []
            if self._fan_limit_active():
                extras.append(f"fan ≤ {self._fan_limit_mode_value()}")
            if self._ds_stale:
                extras.append("ds sensor stale")
            if extras:
                bits.append("(" + ", ".join(extras) + ")")
                # Stalled-and-pushing gets a distinctive icon so it stands
                # out from a normal cycle on the dashboard.
                icon = "mdi:rocket-launch"
            return " ".join(bits), icon

        # Idle inside the deadband — show how close we are to either edge
        # so the user can see the system is "watching" rather than asleep.
        near = 1.0
        if room <= self._heat_target + near:
            gap = room - self._heat_target
            return (
                f"Idle, {gap:+.1f}°F from heat start",
                "mdi:thermometer-chevron-down",
            )
        if room >= self._cool_target - near:
            gap = room - self._cool_target
            return (
                f"Idle, {gap:+.1f}°F from cool start",
                "mdi:thermometer-chevron-up",
            )

        return "Idle", "mdi:thermometer-check"

    @property
    def short_status(self) -> str:
        return self._compute_short_status()[0]

    @property
    def short_status_icon(self) -> str:
        return self._compute_short_status()[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status_text, status_icon = self._compute_short_status()
        return {
            "short_status": status_text,
            "short_status_icon": status_icon,
            "decision_reason": self._decision_reason,
            "active_mode": self._active_mode.value if self._active_mode else None,
            "room_temperature": self._last_room_temp,
            "heat_target": self._heat_target,
            "cool_target": self._cool_target,
            "deadband": self._deadband,
            "settling_time": self._settling_time,
            "settling_remaining_seconds": self._settling_remaining_s(),
            "error_from_band": self._last_error,
            "filtered_room_temperature": self._filtered_room_temp,
            "room_temperature_slope_per_minute": (
                round(self._room_temp_slope, 4)
                if self._room_temp_slope is not None
                else None
            ),
            "projected_room_temperature_5m": self._projected_room_temp,
            "thermal_learning_confidence": round(self._learning_confidence, 2),
            "thermal_learning": self._thermal_learner.as_dict(),
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
            "fan_limit_mode": (
                self._fan_limit_mode_value() if self._fan_limit_active() else None
            ),
            "fan_limit_until": (
                self._fan_limit_until_value().isoformat()
                if self._fan_limit_active() and self._fan_limit_until_value()
                else None
            ),
            "fan_limit_remaining_minutes": self._fan_limit_remaining_minutes(),
            "downstream_sensor_bias": (
                round(self._ds_bias_ema, 2) if self._ds_bias_ema is not None else None
            ),
            "downstream_sensor_stale": self._ds_stale,
            "downstream_sensor_age_s": (
                int((dt_util.utcnow() - self._ds_last_change_at).total_seconds())
                if self._ds_last_change_at
                else None
            ),
            "recent_heat_starts": [
                t.isoformat() for t in self._cycle_starts[HVACMode.HEAT]
            ],
            "recent_cool_starts": [
                t.isoformat() for t in self._cycle_starts[HVACMode.COOL]
            ],
        }

    # ------------------------------------------------------------------ user commands

    async def async_set_temperature(self, **kwargs: Any) -> None:
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        single = kwargs.get(ATTR_TEMPERATURE)
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        # Compute proposed targets without mutating self yet — that way an
        # invalid range leaves the entity in its previous good state.
        new_heat = self._heat_target
        new_cool = self._cool_target
        if low is not None:
            new_heat = float(low)
        if high is not None:
            new_cool = float(high)
        if single is not None and low is None and high is None:
            mid = float(single)
            half = (new_cool - new_heat) / 2 or 2.5
            new_heat = mid - half
            new_cool = mid + half

        if new_heat >= new_cool:
            _LOGGER.warning(
                "Invalid target range (heat %.1f >= cool %.1f), ignoring",
                new_heat,
                new_cool,
            )
            return

        if hvac_mode is not None:
            self._attr_hvac_mode = hvac_mode
        self._heat_target = new_heat
        self._cool_target = new_cool

        self.async_write_ha_state()
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            await self._async_stop_downstream()
            self._active_mode = None
            self._attr_hvac_action = HVACAction.OFF
        elif (self._active_mode == HVACMode.COOL and hvac_mode == HVACMode.HEAT) or (
            self._active_mode == HVACMode.HEAT and hvac_mode == HVACMode.COOL
        ):
            await self._async_stop_downstream()
            self._active_mode = None
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        ds_state = self.hass.states.get(self._downstream)
        available = ds_state.attributes.get(ATTR_FAN_MODES) or [] if ds_state else []
        fan_mode = self._cap_fan_mode(fan_mode, available)
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

    @callback
    def _async_fan_limit_updated(self) -> None:
        """Apply a dashboard fan-limit change without reloading the entity."""
        self.hass.async_create_task(self._async_control())

    async def _async_tick(self, _now: datetime) -> None:
        await self._async_control()

    # ------------------------------------------------------------------ control core

    async def _async_control(self) -> None:
        """Serialize control passes triggered by state changes and the timer."""
        async with self._control_lock:
            await self._async_control_locked()

    async def _async_control_locked(self) -> None:
        """Run one control pass while holding the control lock."""
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

        # Treat the room sensor as lost if it has no value, hasn't updated
        # in over an hour (integration down), or has been reporting the
        # exact same value for 12+ hours (sensor likely offline but the
        # integration is still echoing the last reading — X-Sense does this).
        sensor_issue: str | None = None
        if room_temp is None:
            sensor_issue = "unavailable"
        else:
            room_state = self.hass.states.get(self._source_temp)
            if room_state is not None:
                now_utc = dt_util.utcnow()
                if self._room_sensor_stale_s > 0:
                    age = (now_utc - room_state.last_updated).total_seconds()
                    if age > self._room_sensor_stale_s:
                        sensor_issue = f"stale (no update for >{self._room_sensor_stale_s // 60}min)"
                if sensor_issue is None and self._room_sensor_stuck_s > 0:
                    stuck_age = (now_utc - room_state.last_changed).total_seconds()
                    if stuck_age > self._room_sensor_stuck_s:
                        sensor_issue = f"stuck (value unchanged for >{self._room_sensor_stuck_s // 3600}h)"

        if sensor_issue is not None:
            await self._async_handle_room_sensor_lost(
                ds_state, allow_heat, allow_cool, sensor_issue=sensor_issue
            )
            return

        self._emergency_active = False

        # Keep a short, de-duplicated history from the real room sensor.  The
        # median filters single-sample spikes and the fitted slope lets us stop
        # before a lagging remote sensor carries the room through the target.
        room_state = self.hass.states.get(self._source_temp)
        if room_state is not None:
            self._temperature_tracker.add(room_state.last_changed, room_temp)
        estimate = self._temperature_tracker.estimate()
        control_temp = estimate.filtered if estimate is not None else room_temp
        self._filtered_room_temp = round(control_temp, 2)
        self._room_temp_slope = (
            estimate.slope_per_minute if estimate is not None else None
        )
        self._projected_room_temp = round(
            estimate.projected_5m if estimate is not None else control_temp, 2
        )
        now = dt_util.utcnow()
        active_fan = self._last_fan_tier if self._active_mode else None
        outdoor_temp = self._outdoor_temperature()
        self._thermal_learner.observe(
            now,
            control_temp,
            self._active_mode.value if self._active_mode else None,
            active_fan,
            outdoor_temp,
        )

        desired: HVACMode | None = self._active_mode
        transition_reason: str | None = None

        if desired is None:
            # Idle: decide whether to start a cycle.
            settling_remaining = self._settling_remaining_s()
            if settling_remaining:
                self._decision_reason = (
                    f"SETTLING: observing room mixing for {settling_remaining}s "
                    "before another cycle"
                )
            elif allow_cool and control_temp > self._cool_target + self._deadband:
                desired = HVACMode.COOL
                transition_reason = (
                    f"Starting COOL: filtered room {control_temp:.1f}°F > "
                    "cool_target + deadband "
                    f"({self._cool_target:.1f} + {self._deadband:.1f} = "
                    f"{self._cool_target + self._deadband:.1f}°F)"
                )
            elif allow_heat and control_temp < self._heat_target - self._deadband:
                desired = HVACMode.HEAT
                transition_reason = (
                    f"Starting HEAT: filtered room {control_temp:.1f}°F < "
                    "heat_target − deadband "
                    f"({self._heat_target:.1f} − {self._deadband:.1f} = "
                    f"{self._heat_target - self._deadband:.1f}°F)"
                )
            elif not settling_remaining:
                self._decision_reason = (
                    f"IDLE: filtered room {control_temp:.1f}°F is inside target band "
                    f"{self._heat_target:.1f}–{self._cool_target:.1f}°F "
                    f"(start thresholds <{self._heat_target - self._deadband:.1f} "
                    f"or >{self._cool_target + self._deadband:.1f})"
                )
        elif desired in (HVACMode.HEAT, HVACMode.COOL):
            # Running: use trajectory-aware cutoff.  We never deliberately
            # bank heat/cold beyond the target merely to lengthen an off
            # period; that behavior was the main source of large sawteeth.
            stop_at = (
                self._cool_target if desired == HVACMode.COOL else self._heat_target
            )
            projected, self._learning_confidence = self._thermal_learner.project(
                current=control_temp,
                mode=desired.value,
                fan_mode=self._last_fan_tier,
                live_slope=estimate.slope_per_minute if estimate is not None else None,
                outdoor_temperature=outdoor_temp,
            )
            self._projected_room_temp = round(projected, 2)
            stopped = projected_stop(
                cooling=desired == HVACMode.COOL,
                current=control_temp,
                projected=projected,
                target=stop_at,
            )
            # Suppress the stop check during the post-start sensor-settle
            # window: the downstream blower can spike a nearby room sensor
            # 3-5°F within the first minute or two of running, which would
            # otherwise satisfy stop_at instantly and shut the cycle down
            # before the room mass actually moves.
            settle_remaining = self._start_settle_remaining_s()
            if stopped and settle_remaining > 0:
                stopped = False
                transition_reason = (
                    f"{desired.value.upper()}: projected room "
                    f"{projected:.1f}°F has reached stop {stop_at:.1f}°F, "
                    "but holding for "
                    f"{settle_remaining}s of sensor-settle window "
                    f"({self._start_measurement_delay}s)"
                )
            if stopped:
                transition_reason = (
                    f"Ending {desired.value.upper()}: filtered room "
                    f"{control_temp:.1f}°F, projected {projected:.1f}°F "
                    f"reached stop {stop_at:.1f}°F"
                )
                desired = None

        # Minimum cycle time gate — only blocks turning ON (idle → active or
        # switching between active modes). Turning OFF is always allowed.
        remaining = self._transition_hold_remaining(desired)
        if remaining:
            transition_reason = (
                f"Min cycle hold: wanted to start {desired.value} but "
                f"{remaining}s remain of min_cycle_time ({self._min_cycle}s)"
            )
            desired = self._active_mode
            # The idle-branch early return below would otherwise leave
            # decision_reason stale; surface the hold reason now.
            if desired is None:
                self._decision_reason = transition_reason

        if desired != self._active_mode:
            if self._active_mode is None and desired is not None:
                self._record_cycle_start(desired)
            self._active_mode = desired
            self._last_transition = dt_util.utcnow()
            if desired is None:
                await self._async_stop_downstream()
                self._settle_until = dt_util.utcnow() + timedelta(
                    seconds=self._settling_time
                )
                self._attr_hvac_action = HVACAction.IDLE
                self._last_error = 0.0
                self._last_pushed_setpoint = None
                self._last_fan_tier = None
                self._decision_reason = (
                    f"{transition_reason or 'Stopped'}. "
                    f"Will settle for {self._settling_time}s."
                )
                self.async_write_ha_state()
                return

        if self._active_mode is None:
            self._attr_hvac_action = HVACAction.IDLE
            self._learning_confidence = 0.0
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

        reason = await self._async_drive_active(
            control_temp, self._active_mode, ds_state
        )
        self._decision_reason = (
            f"{transition_reason}. {reason}" if transition_reason else reason
        )
        self._attr_hvac_action = (
            HVACAction.COOLING
            if self._active_mode == HVACMode.COOL
            else HVACAction.HEATING
        )
        self.async_write_ha_state()

    # ------------------------------------------------------------------ cycle tracking

    def _start_settle_remaining_s(self) -> int:
        """Seconds left in the post-start sensor-settle window, or 0."""
        if (
            self._active_mode not in (HVACMode.HEAT, HVACMode.COOL)
            or self._last_transition is None
            or self._start_measurement_delay <= 0
        ):
            return 0
        elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
        remaining = self._start_measurement_delay - elapsed
        return int(remaining) if remaining > 0 else 0

    def _record_cycle_start(self, mode: HVACMode) -> None:
        """Log a cycle start and reset per-cycle actuator state."""
        if mode not in self._cycle_starts:
            return
        now = dt_util.utcnow()
        history = self._cycle_starts[mode]
        history.append(now)
        if len(history) > CYCLE_HISTORY:
            del history[:-CYCLE_HISTORY]

    # ------------------------------------------------------------------ room sensor lost

    async def _async_handle_room_sensor_lost(
        self,
        ds_state: State,
        allow_heat: bool,
        allow_cool: bool,
        sensor_issue: str = "unavailable",
    ) -> None:
        """Emergency fallback when the room sensor is unavailable, stale, or stuck."""
        was_emergency = self._emergency_active

        if not self._emergency_enable:
            if not was_emergency and self._active_mode is not None:
                _LOGGER.warning(
                    "Source temp sensor %s %s and emergency mode "
                    "disabled; turning downstream off",
                    self._source_temp,
                    sensor_issue,
                )
            await self._async_go_idle()
            self._decision_reason = (
                f"Room sensor {self._source_temp} {sensor_issue} and emergency "
                "mode is disabled; downstream turned off for safety."
            )
            self.async_write_ha_state()
            return

        outdoor_temp = self._outdoor_temperature()

        desired: HVACMode | None = None
        if outdoor_temp is not None:
            if allow_heat and outdoor_temp < self._emergency_heat_below:
                desired = HVACMode.HEAT
            elif allow_cool and outdoor_temp > self._emergency_cool_above:
                desired = HVACMode.COOL

        if desired is None:
            if not was_emergency:
                _LOGGER.warning(
                    "Source temp sensor %s %s; emergency conditions "
                    "not met (outdoor=%s), turning downstream off",
                    self._source_temp,
                    sensor_issue,
                    outdoor_temp,
                )
            await self._async_go_idle()
            outdoor_str = (
                f"{outdoor_temp:.1f}°F" if outdoor_temp is not None else "unavailable"
            )
            self._decision_reason = (
                f"EMERGENCY STANDBY: room sensor {self._source_temp} "
                f"{sensor_issue}, outdoor {outdoor_str}. Within safe band "
                f"({self._emergency_heat_below:.0f}–"
                f"{self._emergency_cool_above:.0f}°F), downstream off."
            )
            self.async_write_ha_state()
            return

        # Apply min-cycle gate to emergency transitions too — only on turn-on.
        remaining = self._transition_hold_remaining(desired)
        if remaining:
            if self._active_mode is None:
                self._emergency_active = False
                self._attr_hvac_action = HVACAction.IDLE
                self._decision_reason = (
                    f"Min cycle hold: emergency wants to start {desired.value}, "
                    f"but {remaining}s remain of min_cycle_time "
                    f"({self._min_cycle}s)."
                )
                self.async_write_ha_state()
                return
            desired = self._active_mode

        if desired != self._active_mode:
            self._active_mode = desired
            self._last_transition = dt_util.utcnow()

        self._emergency_active = True
        if not was_emergency:
            _LOGGER.warning(
                "EMERGENCY mode active: room sensor %s %s, "
                "outdoor=%.1f, driving downstream in %s",
                self._source_temp,
                sensor_issue,
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
            f"{self._source_temp} {sensor_issue}, outdoor {outdoor_temp:.1f}°F "
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

        # ---- Bias EMA: track how much the unit's own sensor disagrees
        # with the room sensor for diagnostics only.
        # Skip the EMA update when the downstream sensor is stale
        # (frozen value while the room has clearly moved).
        now = dt_util.utcnow()
        ds_current = _as_float_attr(ds_state.attributes.get("current_temperature"))
        ds_stale = False
        if ds_current is not None:
            if self._ds_last_value is None or ds_current != self._ds_last_value:
                # Fresh value — record and clear staleness.
                self._ds_last_value = ds_current
                self._ds_last_change_at = now
                self._ds_last_change_room_temp = room_temp
            else:
                # Same value as last time — check if it's been stuck
                # while the room moved meaningfully.
                age = (
                    (now - self._ds_last_change_at).total_seconds()
                    if self._ds_last_change_at
                    else 0.0
                )
                baseline = self._ds_last_change_room_temp
                room_delta = abs(room_temp - baseline) if baseline is not None else 0.0
                if age > BIAS_STALE_AFTER_S and room_delta > BIAS_STALE_ROOM_DELTA:
                    ds_stale = True

            if not ds_stale:
                raw_bias = ds_current - room_temp
                if self._ds_bias_ema is None:
                    self._ds_bias_ema = raw_bias
                else:
                    self._ds_bias_ema = (
                        BIAS_EMA_ALPHA * raw_bias
                        + (1 - BIAS_EMA_ALPHA) * self._ds_bias_ema
                    )
        self._ds_stale = ds_stale

        if mode == HVACMode.COOL:
            error = max(0.0, room_temp - self._cool_target)
            active_offset = gentle_setpoint_offset(error, self._offset)
            raw_setpoint = self._cool_target - active_offset
        else:
            error = max(0.0, self._heat_target - room_temp)
            active_offset = gentle_setpoint_offset(error, self._offset)
            raw_setpoint = self._heat_target + active_offset

        setpoint = self._clamp(raw_setpoint, ds_min, ds_max, ds_step)
        available_fan = ds_state.attributes.get(ATTR_FAN_MODES) or []

        fan_mode = self._pick_fan_mode(error, available_fan)
        fan_mode = self._cap_fan_mode(fan_mode, available_fan)

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

        bias_note = ""
        if self._ds_bias_ema is not None:
            stale_marker = " STALE" if self._ds_stale else ""
            bias_note = (
                f" Unit sensor bias {self._ds_bias_ema:+.1f}°F{stale_marker}"
                " (diagnostic only)."
            )
        elif ds_current is None:
            bias_note = " Unit sensor not reported."
        trend_note = ""
        if self._room_temp_slope is not None:
            trend_note = (
                f" Trend {self._room_temp_slope:+.3f}°F/min; "
                f"5m projection {self._projected_room_temp:.1f}°F."
            )
        limit_note = (
            f" Temporary fan limit: ≤{self._fan_limit_mode_value()}."
            if self._fan_limit_active()
            else ""
        )

        return (
            f"{mode.value.upper()}ING: room {room_temp:.1f}°F, {target_label}, "
            f"error {error:.1f}°F. Pushing downstream setpoint to "
            f"{setpoint:.0f}°F (target {offset_sign} {active_offset:.1f}°F "
            "gentle offset, "
            f"clamped to {ds_min:.0f}–{ds_max:.0f}). "
            f"Fan tier: {fan_mode or 'n/a'}.{bias_note}{trend_note}"
            f"{limit_note} {stop_label}."
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

        # A mismatch with no pending command means something external changed
        # the unit after our previous write was confirmed.
        if cur_mode != desired_hvac and "hvac_mode" not in self._last_sent:
            _LOGGER.warning(
                "Downstream %s is '%s' (expected '%s'); " "re-asserting control",
                self._downstream,
                cur_mode,
                desired_hvac,
            )

        if self._should_send("hvac_mode", cur_mode, desired_hvac):
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self._downstream, "hvac_mode": desired_hvac},
                blocking=True,
            )
            self._last_sent["hvac_mode"] = desired_hvac

        if self._should_send("setpoint", cur_setpoint, setpoint):
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

        if fan_mode and self._should_send("fan_mode", cur_fan, fan_mode):
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

    def _outdoor_temperature(self) -> float | None:
        """Return the optional outdoor sensor value when usable."""
        if not self._outdoor_sensor:
            return None
        return _as_float(self.hass.states.get(self._outdoor_sensor))

    def _transition_hold_remaining(self, desired: HVACMode | None) -> int:
        """Return seconds remaining before a mode transition may start."""
        if (
            desired is None
            or desired == self._active_mode
            or self._last_transition is None
        ):
            return 0
        elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
        return max(0, math.ceil(self._min_cycle - elapsed))

    def _settling_remaining_s(self) -> int:
        """Return seconds left in the post-shutdown room-mixing period."""
        if self._settle_until is None:
            return 0
        remaining = (self._settle_until - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            self._settle_until = None
            return 0
        return math.ceil(remaining)

    def _should_send(self, key: str, current: Any, desired: Any) -> bool:
        """Deduplicate pending writes while still correcting later drift."""
        if current == desired:
            # The downstream confirmed an earlier write. Removing the pending
            # marker lets a subsequent manual/cloud change be re-asserted.
            self._last_sent.pop(key, None)
            return False
        return self._last_sent.get(key) != desired

    def _fan_limit_active(self) -> bool:
        """Return whether the temporary fan cap is still active."""
        mode = self._fan_limit_mode_value()
        until = self._fan_limit_until_value()
        return bool(mode) and until is not None and dt_util.utcnow() < until

    def _fan_limit_remaining_minutes(self) -> int:
        """Return whole minutes remaining on the temporary fan cap."""
        until = self._fan_limit_until_value()
        if not self._fan_limit_active() or until is None:
            return 0
        seconds = (until - dt_util.utcnow()).total_seconds()
        return max(0, math.ceil(seconds / 60))

    def _fan_limit_mode_value(self) -> str | None:
        """Return the current live fan-limit mode from config-entry options."""
        value = self._entry.options.get(CONF_FAN_LIMIT_MODE)
        return str(value) if value else None

    def _fan_limit_until_value(self) -> datetime | None:
        """Return the current live fan-limit expiration."""
        return dt_util.parse_datetime(self._entry.options.get(CONF_FAN_LIMIT_UNTIL, ""))

    def _cap_fan_mode(self, requested: str | None, available: list[str]) -> str | None:
        """Apply the temporary cap to a normal-operation fan mode."""
        if not requested or not self._fan_limit_active():
            return requested
        limit = self._fan_limit_mode_value()
        if limit not in available:
            return requested

        ranked = [
            tier["fan_mode"]
            for tier in self._fan_tiers
            if tier["fan_mode"] in available
        ]
        ranked = list(dict.fromkeys(ranked))
        if limit not in ranked:
            return limit
        if requested not in ranked:
            return limit
        return ranked[min(ranked.index(requested), ranked.index(limit))]

    def _pick_fan_mode(self, error: float, available: list[str]) -> str | None:
        if not available:
            return None
        # Build the list of tiers whose fan mode is actually offered by
        # the downstream device (in error-ascending order).
        usable = [t for t in self._fan_tiers if t["fan_mode"] in available]
        if not usable:
            for tier in reversed(self._fan_tiers):
                if tier["fan_mode"] in available:
                    return tier["fan_mode"]
            return available[0]
        # Find the natural index for the current error.
        natural_idx = len(usable) - 1
        for idx, tier in enumerate(usable):
            if error <= tier["max_error"]:
                natural_idx = idx
                break
        return usable[natural_idx]["fan_mode"]

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
        return max(low, min(high, value))
