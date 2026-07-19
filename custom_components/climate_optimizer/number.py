"""Dashboard control for temporary fan-limit duration."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_FAN_LIMIT_HOURS,
    CONF_FAN_LIMIT_MAX_HOURS,
    CONF_FAN_LIMIT_MODE,
    DEFAULT_FAN_LIMIT_HOURS,
    DEFAULT_FAN_LIMIT_MAX_HOURS,
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
    _attr_native_step = 0.5

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_fan_limit_hours"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> float:
        configured = (
            float(self._entry.options.get(CONF_FAN_LIMIT_HOURS, 0))
            or DEFAULT_FAN_LIMIT_HOURS
        )
        return min(configured, self.native_max_value)

    @property
    def native_max_value(self) -> float:
        """Return the dashboard ceiling configured in advanced options."""
        return float(
            self._entry.options.get(
                CONF_FAN_LIMIT_MAX_HOURS, DEFAULT_FAN_LIMIT_MAX_HOURS
            )
        )

    async def async_added_to_hass(self) -> None:
        """Refresh when fan-limit options change without a reload."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                fan_limit_signal(self._entry.entry_id),
                self._async_limit_updated,
            )
        )

    @callback
    def _async_limit_updated(self) -> None:
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Save the duration and restart an active fan-limit timer."""
        value = min(value, self.native_max_value)
        mode = (
            str(self._entry.options[CONF_FAN_LIMIT_MODE])
            if fan_limit_is_active(self._entry.options)
            else None
        )
        update_fan_limit(self.hass, self._entry, mode=mode, hours=value)
