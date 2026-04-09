"""Companion status sensor for the Climate Optimizer virtual climate device.

The sensor is a thin reflector of `short_status` / `short_status_icon`
attributes computed on the climate entity. Keeping the derivation in
climate.py means there's a single source of truth, and the sensor
re-renders whenever the climate entity writes state.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status sensor for a config entry."""
    merged = {**entry.data, **entry.options}
    async_add_entities([ClimateOptimizerStatusSensor(entry, merged)])


class ClimateOptimizerStatusSensor(SensorEntity):
    """Reflects the climate entity's short_status as a first-class sensor."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, cfg: dict[str, Any]) -> None:
        self._entry_id = entry.entry_id
        name = cfg[CONF_NAME]
        self._attr_name = f"{name} Status"
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )
        self._climate_entity_id: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        # Resolve the sibling climate entity from the registry — both
        # entities share the same config entry and the climate's
        # unique_id is deterministic.
        ent_reg = er.async_get(self.hass)
        self._climate_entity_id = ent_reg.async_get_entity_id(
            "climate", DOMAIN, f"{self._entry_id}_virtual_climate"
        )

        if self._climate_entity_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._climate_entity_id],
                    self._async_climate_changed,
                )
            )

    @callback
    def _async_climate_changed(self, _event: Event) -> None:
        self.async_write_ha_state()

    def _climate_attr(self, key: str) -> Any:
        if not self._climate_entity_id:
            return None
        state = self.hass.states.get(self._climate_entity_id)
        if state is None:
            return None
        return state.attributes.get(key)

    @property
    def native_value(self) -> str | None:
        return self._climate_attr("short_status")

    @property
    def icon(self) -> str | None:
        return self._climate_attr("short_status_icon") or "mdi:thermometer"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "decision_reason": self._climate_attr("decision_reason"),
        }

    @property
    def available(self) -> bool:
        return self._climate_entity_id is not None and self.hass.states.get(
            self._climate_entity_id
        ) is not None
