"""Dashboard control for the temporary maximum fan speed."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from homeassistant.components.climate import ATTR_FAN_MODES
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DOWNSTREAM_CLIMATE,
    CONF_FAN_LIMIT_HOURS,
    CONF_FAN_LIMIT_MAX_HOURS,
    CONF_FAN_LIMIT_MODE,
    CONF_FAN_LIMIT_UNTIL,
    DEFAULT_FAN_LIMIT_HOURS,
    DEFAULT_FAN_LIMIT_MAX_HOURS,
    FAN_LIMIT_DISABLED,
    FAN_TIER_KEYS,
)
from .fan_limit import (
    device_info,
    fan_limit_is_active,
    fan_limit_signal,
    update_fan_limit,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the maximum fan-speed select."""
    async_add_entities([FanLimitSelect(entry)])


class FanLimitSelect(SelectEntity):
    """Select and activate a temporary maximum fan speed."""

    _attr_has_entity_name = True
    _attr_name = "Maximum Fan Speed"
    _attr_icon = "mdi:fan-chevron-down"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._cancel_expiry: Callable[[], None] | None = None
        self._attr_unique_id = f"{entry.entry_id}_fan_limit_mode"
        self._attr_device_info = device_info(entry)

    @property
    def options(self) -> list[str]:
        """Return Disabled plus downstream/configured fan modes."""
        merged = {**self._entry.data, **self._entry.options}
        values = [
            merged.get(mode_key, mode_default)
            for _, _, mode_key, mode_default in FAN_TIER_KEYS
        ]
        downstream = self.hass.states.get(merged[CONF_DOWNSTREAM_CLIMATE])
        if downstream is not None:
            values.extend(downstream.attributes.get(ATTR_FAN_MODES) or [])
        saved = merged.get(CONF_FAN_LIMIT_MODE)
        if saved:
            values.append(saved)
        return [FAN_LIMIT_DISABLED, *dict.fromkeys(str(value) for value in values)]

    @property
    def current_option(self) -> str:
        """Return the active cap, or Disabled after it expires."""
        return (
            str(self._entry.options[CONF_FAN_LIMIT_MODE])
            if fan_limit_is_active(self._entry.options)
            else FAN_LIMIT_DISABLED
        )

    async def async_added_to_hass(self) -> None:
        """Refresh the displayed selection when the cap expires."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                fan_limit_signal(self._entry.entry_id),
                self._async_limit_updated,
            )
        )
        self.async_on_remove(self._cancel_expiry_tracking)
        self._schedule_expiry()

    @callback
    def _async_limit_updated(self) -> None:
        """Refresh the entity and expiration callback after a live update."""
        self._schedule_expiry()
        self.async_write_ha_state()

    @callback
    def _schedule_expiry(self) -> None:
        """Schedule a state refresh at the current limit's expiration."""
        self._cancel_expiry_tracking()
        until = dt_util.parse_datetime(
            self._entry.options.get(CONF_FAN_LIMIT_UNTIL, "")
        )
        if until is not None and until > dt_util.utcnow():
            self._cancel_expiry = async_track_point_in_utc_time(
                self.hass, self._async_limit_expired, until
            )

    @callback
    def _async_limit_expired(self, _now: datetime) -> None:
        self._cancel_expiry = None
        self.async_write_ha_state()

    @callback
    def _cancel_expiry_tracking(self) -> None:
        if self._cancel_expiry is not None:
            self._cancel_expiry()
            self._cancel_expiry = None

    async def async_select_option(self, option: str) -> None:
        """Activate the selected cap or disable it."""
        hours = (
            float(self._entry.options.get(CONF_FAN_LIMIT_HOURS, 0))
            or DEFAULT_FAN_LIMIT_HOURS
        )
        max_hours = float(
            self._entry.options.get(
                CONF_FAN_LIMIT_MAX_HOURS, DEFAULT_FAN_LIMIT_MAX_HOURS
            )
        )
        hours = min(hours, max_hours)
        update_fan_limit(
            self.hass,
            self._entry,
            mode=None if option == FAN_LIMIT_DISABLED else option,
            hours=hours,
        )
