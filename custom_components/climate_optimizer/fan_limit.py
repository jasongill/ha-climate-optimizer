"""Shared helpers for dashboard fan-limit controls."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FAN_LIMIT_MODE,
    CONF_FAN_LIMIT_HOURS,
    CONF_FAN_LIMIT_UNTIL,
    DOMAIN,
)


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the shared virtual-device identity."""
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})


def fan_limit_is_active(options: dict[str, Any]) -> bool:
    """Return whether the options contain an unexpired fan limit."""
    until = dt_util.parse_datetime(options.get(CONF_FAN_LIMIT_UNTIL, ""))
    return (
        bool(options.get(CONF_FAN_LIMIT_MODE))
        and until is not None
        and dt_util.utcnow() < until
    )


def update_fan_limit(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    mode: str | None,
    hours: float,
) -> None:
    """Persist a fan limit; the entry update listener reloads all platforms."""
    options = {**entry.options}
    options[CONF_FAN_LIMIT_HOURS] = hours
    if mode is None:
        options.pop(CONF_FAN_LIMIT_MODE, None)
        options.pop(CONF_FAN_LIMIT_UNTIL, None)
    else:
        options[CONF_FAN_LIMIT_MODE] = mode
        options[CONF_FAN_LIMIT_UNTIL] = (
            dt_util.utcnow() + timedelta(hours=hours)
        ).isoformat()
    hass.config_entries.async_update_entry(entry, options=options)
