"""Virtual climate entity that drives a downstream climate device."""
from __future__ import annotations

import logging
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
    CONF_HEAT_TARGET,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_ROOM_SENSOR_STALE_MINUTES,
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
    DEFAULT_SETPOINT_OFFSET,
    DEFAULT_START_MEASUREMENT_DELAY,
    DEFAULT_TICK_INTERVAL,
    DOMAIN,
    FAN_TIER_KEYS,
)

_LOGGER = logging.getLogger(__name__)

# Adaptive overshoot tuning. When the gap between successive starts of the
# same mode is shorter than ADAPTIVE_TARGET_PERIOD_S, the mode is short-
# cycling and we extend its stop threshold by ADAPTIVE_BUMP °F (so heat runs
# a bit past heat_target, cool runs a bit past cool_target). When a cycle
# gap is comfortably long (> 2× target), we decay back toward 0. Capped so
# we never overshoot more than ADAPTIVE_MAX °F.
#
# BUMP is 0.5 to match the typical 0.5°F room-sensor resolution — anything
# smaller would let the internal threshold drift between sensor ticks
# without changing observable stop behavior. DECAY is asymmetrically
# smaller so learning persists across the night and only fades when
# conditions clearly improve.
ADAPTIVE_TARGET_PERIOD_S = 30 * 60
ADAPTIVE_BUMP = 0.5
ADAPTIVE_DECAY = 0.25
ADAPTIVE_MAX = 2.0
ADAPTIVE_HISTORY = 4

# Fan boost tuning. Within an active cycle we sample progress every
# FAN_PROGRESS_INTERVAL_S; if the room temperature has improved by less
# than FAN_PROGRESS_MIN_DELTA °F over that interval (or has gotten worse),
# we bump the fan tier up by one slot. The boost resets at the start of
# every new cycle since the room dynamics may have changed.
FAN_PROGRESS_INTERVAL_S = 5 * 60
FAN_PROGRESS_MIN_DELTA = 0.5
FAN_BOOST_MAX = 4

# Setpoint boost: when stalled, push the downstream setpoint further past
# our own target before falling back to the noisy fan boost. Inverters
# scale compressor speed with the perceived setpoint delta, so an extra
# °F or two of push directly increases BTU/min at no comfort cost.
SETPOINT_BOOST_STEP = 1.0
SETPOINT_BOOST_MAX = 4.0

# Downstream temperature bias: the minisplit's own sensor often reads
# warmer in heat / colder in cool than the actual room (high mounting,
# self-heating, lag). We smooth the delta with an EMA and add it to the
# pushed setpoint so the unit's *perceived* gap matches our intent.
# Compensation only applies in the direction that makes the unit work
# harder — never softer — and is capped to avoid runaway pushes.
BIAS_EMA_ALPHA = 0.2
BIAS_MAX_COMPENSATION = 10.0

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

        self._heat_target = float(cfg.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET))
        self._cool_target = float(cfg.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET))
        self._deadband = float(cfg.get(CONF_DEADBAND, DEFAULT_DEADBAND))
        self._offset = float(cfg.get(CONF_SETPOINT_OFFSET, DEFAULT_SETPOINT_OFFSET))
        self._min_cycle = int(cfg.get(CONF_MIN_CYCLE_TIME, DEFAULT_MIN_CYCLE_TIME))
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
            int(cfg.get(CONF_ROOM_SENSOR_STALE_MINUTES, DEFAULT_ROOM_SENSOR_STALE_MINUTES))
            * 60
        )
        self._fan_tiers = _build_fan_tiers(cfg)

        self._emergency_enable = bool(
            cfg.get(CONF_EMERGENCY_ENABLE, DEFAULT_EMERGENCY_ENABLE)
        )
        self._emergency_heat_below = float(
            cfg.get(CONF_EMERGENCY_HEAT_BELOW_OUTDOOR, DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR)
        )
        self._emergency_cool_above = float(
            cfg.get(CONF_EMERGENCY_COOL_ABOVE_OUTDOOR, DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR)
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
        self._last_sent: dict[str, Any] = {}

        # Adaptive overshoot — recent start timestamps per mode and the
        # current per-mode overshoot in °F applied to the stop threshold.
        self._cycle_starts: dict[HVACMode, list[datetime]] = {
            HVACMode.HEAT: [],
            HVACMode.COOL: [],
        }
        self._overshoot: dict[HVACMode, float] = {
            HVACMode.HEAT: 0.0,
            HVACMode.COOL: 0.0,
        }

        # Fan boost — within-cycle escalation when progress is stalled.
        self._fan_boost: int = 0
        self._setpoint_boost: float = 0.0
        self._progress_last_check: datetime | None = None
        self._progress_last_error: float | None = None

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
                _LOGGER.debug(
                    "Could not restore target temps from %s/%s", low, high
                )
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

            # Restore learned adaptive state. We persist only the values
            # that represent slow-changing physical realities — overshoot
            # (room thermal behavior) and bias EMA (install geometry).
            # Within-cycle boost state and staleness tracking re-bootstrap
            # naturally within a few ticks, so they're not restored.
            for mode, key in (
                (HVACMode.HEAT, "adaptive_heat_overshoot"),
                (HVACMode.COOL, "adaptive_cool_overshoot"),
            ):
                restored = _as_float_attr(attrs.get(key))
                if restored is not None:
                    self._overshoot[mode] = max(0.0, min(ADAPTIVE_MAX, restored))
            restored_bias = _as_float_attr(attrs.get("downstream_sensor_bias"))
            if restored_bias is not None:
                self._ds_bias_ema = restored_bias

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
                target = self._heat_target + self._overshoot[HVACMode.HEAT]
                bits = [f"Heating → {target:.0f}°F"]
                icon = "mdi:fire"
            else:
                target = self._cool_target - self._overshoot[HVACMode.COOL]
                bits = [f"Cooling → {target:.0f}°F"]
                icon = "mdi:snowflake"
            settle_remaining = self._start_settle_remaining_s()
            if settle_remaining > 0:
                bits.append(f"(settling sensor {settle_remaining}s)")
                icon = "mdi:timer-sand-paused"
            extras: list[str] = []
            if self._setpoint_boost:
                extras.append(f"+{self._setpoint_boost:.0f}° push")
            if self._fan_boost:
                extras.append(f"fan+{self._fan_boost}")
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

        # Comfortably mid-band. Surface adaptive learning if any so the
        # user knows the system has been tuning itself.
        learned = max(
            self._overshoot[HVACMode.HEAT], self._overshoot[HVACMode.COOL]
        )
        if learned > 0:
            return (
                f"Idle (learned +{learned:.1f}° overshoot)",
                "mdi:school",
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
            "adaptive_heat_overshoot": round(self._overshoot[HVACMode.HEAT], 2),
            "adaptive_cool_overshoot": round(self._overshoot[HVACMode.COOL], 2),
            "fan_boost": self._fan_boost,
            "setpoint_boost": self._setpoint_boost,
            "downstream_sensor_bias": (
                round(self._ds_bias_ema, 2)
                if self._ds_bias_ema is not None
                else None
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

        # Treat the room sensor as lost if it has a value but hasn't
        # updated in over an hour — the reading is too stale to trust.
        room_sensor_stale = False
        if room_temp is not None:
            room_state = self.hass.states.get(self._source_temp)
            if room_state is not None:
                age = (
                    dt_util.utcnow() - room_state.last_updated
                ).total_seconds()
                if self._room_sensor_stale_s > 0 and age > self._room_sensor_stale_s:
                    room_sensor_stale = True

        if room_temp is None or room_sensor_stale:
            await self._async_handle_room_sensor_lost(
                ds_state, allow_heat, allow_cool, stale=room_sensor_stale
            )
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
        elif desired in (HVACMode.HEAT, HVACMode.COOL):
            # Running: stop when we reach the target edge, optionally
            # extended by an adaptive overshoot. The overshoot grows when
            # the same mode has been short-cycling and decays when cycles
            # are comfortably long, so a leaky room naturally banks more
            # thermal mass per cycle without the user changing settings.
            overshoot = self._overshoot[desired]
            if desired == HVACMode.COOL:
                stop_at = self._cool_target - overshoot
                stopped = room_temp <= stop_at
            else:
                stop_at = self._heat_target + overshoot
                stopped = room_temp >= stop_at
            # Suppress the stop check during the post-start sensor-settle
            # window: the downstream blower can spike a nearby room sensor
            # 3-5°F within the first minute or two of running, which would
            # otherwise satisfy stop_at instantly and shut the cycle down
            # before the room mass actually moves.
            settle_remaining = self._start_settle_remaining_s()
            if stopped and settle_remaining > 0:
                stopped = False
                transition_reason = (
                    f"{desired.value.upper()}: room {room_temp:.1f}°F already "
                    f"≥ stop {stop_at:.1f}°F but holding for "
                    f"{settle_remaining}s of sensor-settle window "
                    f"({self._start_measurement_delay}s)"
                )
            if stopped:
                overshoot_note = (
                    f" (adaptive overshoot {overshoot:.1f}°F)"
                    if overshoot > 0
                    else ""
                )
                transition_reason = (
                    f"Ending {desired.value.upper()}: room {room_temp:.1f}°F "
                    f"reached stop {stop_at:.1f}°F{overshoot_note}"
                )
                desired = None

        # Minimum cycle time gate — only blocks turning ON (idle → active or
        # switching between active modes). Turning OFF is always allowed.
        if (
            desired is not None
            and desired != self._active_mode
            and self._last_transition is not None
        ):
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                remaining = int(self._min_cycle - elapsed)
                transition_reason = (
                    f"Min cycle hold: wanted to start "
                    f"{desired.value} but {remaining}s "
                    f"remain of min_cycle_time ({self._min_cycle}s)"
                )
                desired = self._active_mode
                # The idle-branch early return below would otherwise leave
                # decision_reason stale; surface the hold reason now.
                if desired is None:
                    self._decision_reason = transition_reason

        if desired != self._active_mode:
            if self._active_mode is None and desired is not None:
                self._record_cycle_start_and_adapt(desired)
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

    # ------------------------------------------------------------------ adaptive overshoot

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

    def _record_cycle_start_and_adapt(self, mode: HVACMode) -> None:
        """Log a cycle start and nudge the per-mode overshoot."""
        if mode not in self._overshoot:
            return
        now = dt_util.utcnow()
        history = self._cycle_starts[mode]
        if history:
            gap = (now - history[-1]).total_seconds()
            if gap < ADAPTIVE_TARGET_PERIOD_S:
                # Short-cycling: extend stop threshold to bank more thermal
                # mass and stretch the next off-period.
                self._overshoot[mode] = min(
                    ADAPTIVE_MAX, self._overshoot[mode] + ADAPTIVE_BUMP
                )
            elif gap > 2 * ADAPTIVE_TARGET_PERIOD_S:
                # Comfortably long gap — relax overshoot back toward zero.
                self._overshoot[mode] = max(
                    0.0, self._overshoot[mode] - ADAPTIVE_DECAY
                )
        history.append(now)
        if len(history) > ADAPTIVE_HISTORY:
            del history[:-ADAPTIVE_HISTORY]

        # Fresh cycle — reset within-cycle boost state. Bias EMA is
        # NOT reset; it's a property of the install, not the cycle.
        self._fan_boost = 0
        self._setpoint_boost = 0.0
        self._progress_last_check = None
        self._progress_last_error = None

    # ------------------------------------------------------------------ room sensor lost

    async def _async_handle_room_sensor_lost(
        self,
        ds_state: State,
        allow_heat: bool,
        allow_cool: bool,
        stale: bool = False,
    ) -> None:
        """Emergency fallback when the room sensor is unavailable or stale."""
        was_emergency = self._emergency_active
        sensor_issue = (
            f"stale (no update for >{self._room_sensor_stale_s // 60}min)"
            if stale
            else "unavailable"
        )

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
        if (
            desired is not None
            and desired != self._active_mode
            and self._last_transition is not None
        ):
            elapsed = (dt_util.utcnow() - self._last_transition).total_seconds()
            if elapsed < self._min_cycle:
                desired = self._active_mode or desired

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
        # with the room sensor, and apply it to the setpoint so the
        # unit's *perceived* gap matches what we actually want.
        # Skip the EMA update when the downstream sensor is stale
        # (frozen value while the room has clearly moved).
        now = dt_util.utcnow()
        ds_current = _as_float_attr(ds_state.attributes.get("current_temperature"))
        ds_stale = False
        if ds_current is not None:
            if (
                self._ds_last_value is None
                or ds_current != self._ds_last_value
            ):
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
                room_delta = abs(
                    room_temp - (self._ds_last_change_room_temp or room_temp)
                )
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

        # Compensation only in the "make it work harder" direction.
        # For heat, a positive bias (unit reads warmer than reality) hurts;
        # for cool, a negative bias (unit reads colder) hurts. Either way
        # we add the absolute hurt to the setpoint push, capped.
        bias_compensation = 0.0
        if self._ds_bias_ema is not None:
            signed = (
                self._ds_bias_ema if mode == HVACMode.HEAT else -self._ds_bias_ema
            )
            bias_compensation = max(0.0, min(BIAS_MAX_COMPENSATION, signed))

        if mode == HVACMode.COOL:
            raw_setpoint = (
                self._cool_target - self._offset
                - bias_compensation - self._setpoint_boost
            )
            error = max(0.0, room_temp - self._cool_target)
        else:
            raw_setpoint = (
                self._heat_target + self._offset
                + bias_compensation + self._setpoint_boost
            )
            error = max(0.0, self._heat_target - room_temp)

        setpoint = self._clamp(raw_setpoint, ds_min, ds_max, ds_step)
        available_fan = ds_state.attributes.get(ATTR_FAN_MODES) or []

        # ---- Stalled-progress detection. On each stall window we escalate
        # ONE lever, preferring the cheapest first: setpoint boost (free,
        # makes the inverter modulate harder) before fan boost (noisy).
        if self._progress_last_check is None:
            self._progress_last_check = now
            self._progress_last_error = error
        else:
            elapsed = (now - self._progress_last_check).total_seconds()
            if elapsed >= FAN_PROGRESS_INTERVAL_S:
                prior_error = (
                    self._progress_last_error
                    if self._progress_last_error is not None
                    else error
                )
                improvement = prior_error - error
                stalled = error > 0.0 and improvement < FAN_PROGRESS_MIN_DELTA
                if stalled:
                    if self._setpoint_boost < SETPOINT_BOOST_MAX:
                        self._setpoint_boost = min(
                            SETPOINT_BOOST_MAX,
                            self._setpoint_boost + SETPOINT_BOOST_STEP,
                        )
                    elif self._fan_boost < FAN_BOOST_MAX:
                        self._fan_boost += 1
                self._progress_last_check = now
                self._progress_last_error = error

        fan_mode = self._pick_fan_mode(error, available_fan, self._fan_boost)

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
                f"{f' (compensated +{bias_compensation:.1f})' if bias_compensation else ''}."
            )
        elif ds_current is None:
            bias_note = " Unit sensor not reported."
        boost_note = ""
        if self._setpoint_boost or self._fan_boost:
            parts = []
            if self._setpoint_boost:
                parts.append(f"setpoint +{self._setpoint_boost:.0f}°F")
            if self._fan_boost:
                parts.append(f"fan +{self._fan_boost}")
            boost_note = f" Stall boosts: {', '.join(parts)}."

        return (
            f"{mode.value.upper()}ING: room {room_temp:.1f}°F, {target_label}, "
            f"error {error:.1f}°F. Pushing downstream setpoint to "
            f"{setpoint:.0f}°F (target {offset_sign} {self._offset:.0f}°F offset, "
            f"clamped to {ds_min:.0f}–{ds_max:.0f}). "
            f"Fan tier: {fan_mode or 'n/a'}.{bias_note}{boost_note} {stop_label}."
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

    def _pick_fan_mode(
        self, error: float, available: list[str], boost: int = 0
    ) -> str | None:
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
        # Apply boost: shift toward the louder end of the list.
        boosted_idx = min(natural_idx + max(0, boost), len(usable) - 1)
        return usable[boosted_idx]["fan_mode"]

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
