"""Dashboard control for temporary fan-limit duration."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FAN_LIMIT_HOURS,
    CONF_FAN_LIMIT_MODE,
    DEFAULT_FAN_LIMIT_HOURS,
)
from .fan_limit import device_info, fan_limit_is_active, update_fan_limit


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan-limit duration number."""
    async_add_entities([FanLimitDurationNumber(entry)])


class FanLimitDurationNumber(NumberEntity):
    """Choose how long selecting a maximum fan speed should last."""

    _attr_has_entity_name = True
    _attr_name = "Fan Limit Duration"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0.5
    _attr_native_max_value = 168
    _attr_native_step = 0.5

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fan_limit_hours"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        return (
            float(self._entry.options.get(CONF_FAN_LIMIT_HOURS, 0))
            or DEFAULT_FAN_LIMIT_HOURS
        )

    async def async_set_native_value(self, value: float) -> None:
        """Save the duration and restart an active fan-limit timer."""
        mode = (
            str(self._entry.options[CONF_FAN_LIMIT_MODE])
            if fan_limit_is_active(self._entry.options)
            else None
        )
        update_fan_limit(self.hass, self._entry, mode=mode, hours=value)
