"""Climate Optimizer integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONFIG_VERSION, FAN_TIER_KEYS

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Climate Optimizer config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries forward."""
    _LOGGER.debug(
        "Migrating config entry %s from version %s", entry.entry_id, entry.version
    )

    if entry.version < 2:
        # v1 stored the fan tier list as a "fan_tiers" list of dicts. v2
        # stores them as four flat (error, mode) pairs editable in the UI.
        data = {**entry.data}
        legacy = data.pop("fan_tiers", None) or []

        for i, (err_key, err_default, mode_key, mode_default) in enumerate(
            FAN_TIER_KEYS
        ):
            if i < len(legacy):
                data.setdefault(
                    err_key, float(legacy[i].get("max_error", err_default))
                )
                data.setdefault(
                    mode_key, str(legacy[i].get("fan_mode", mode_default))
                )
            else:
                data.setdefault(err_key, err_default)
                data.setdefault(mode_key, mode_default)

        hass.config_entries.async_update_entry(entry, data=data, version=CONFIG_VERSION)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
