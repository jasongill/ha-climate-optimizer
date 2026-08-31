"""Config flow for Climate Optimizer."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import ATTR_FAN_MODES
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
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
    CONF_FAN_LIMIT_HOURS,
    CONF_FAN_LIMIT_MAX_HOURS,
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
    CONFIG_VERSION,
    DEFAULT_COOL_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
    DEFAULT_EMERGENCY_COOL_SETPOINT,
    DEFAULT_EMERGENCY_ENABLE,
    DEFAULT_EMERGENCY_FAN_MODE,
    DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
    DEFAULT_EMERGENCY_HEAT_SETPOINT,
    DEFAULT_FAN_LIMIT_HOURS,
    DEFAULT_FAN_LIMIT_MAX_HOURS,
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


def _number(minimum: float = 0) -> vol.All:
    """Return a reusable non-negative numeric validator."""
    return vol.All(vol.Coerce(float), vol.Range(min=minimum))


def _integer(minimum: int = 0) -> vol.All:
    """Return a reusable bounded integer validator."""
    return vol.All(vol.Coerce(int), vol.Range(min=minimum))


def _fan_mode_options(
    hass: HomeAssistant,
    downstream_entity_id: str | None,
    saved_values: list[str],
) -> list[str] | None:
    """Return the union of the downstream climate's fan_modes and any saved
    values, or None if the climate has no usable fan_modes attribute."""
    if not downstream_entity_id:
        return None
    state = hass.states.get(downstream_entity_id)
    if state is None:
        return None
    fan_modes = state.attributes.get(ATTR_FAN_MODES)
    if not fan_modes:
        return None
    options = [str(m) for m in fan_modes]
    for value in saved_values:
        if value and value not in options:
            options.append(value)
    return options


def _fan_mode_field(
    fan_options: list[str] | None,
) -> Any:
    """Validator/selector for a single fan-mode field."""
    if fan_options is None:
        return str
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=fan_options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            custom_value=True,
        )
    )


def _fan_tier_fields(
    current: dict[str, Any], fan_options: list[str] | None
) -> dict[Any, Any]:
    fields: dict[Any, Any] = {}
    for err_key, err_default, mode_key, mode_default in FAN_TIER_KEYS:
        fields[vol.Required(err_key, default=current.get(err_key, err_default))] = (
            _number()
        )
        fields[vol.Required(mode_key, default=current.get(mode_key, mode_default))] = (
            _fan_mode_field(fan_options)
        )
    return fields


def _target_fields(values: dict[str, Any]) -> dict[Any, Any]:
    """Heat/cool target fields — used in both setup and options."""
    return {
        vol.Required(
            CONF_HEAT_TARGET,
            default=values.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET),
        ): vol.Coerce(float),
        vol.Required(
            CONF_COOL_TARGET,
            default=values.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET),
        ): vol.Coerce(float),
    }


def _advanced_control_fields(values: dict[str, Any]) -> dict[Any, Any]:
    """Timing and tuning knobs — only shown in options."""
    return {
        vol.Required(
            CONF_DEADBAND,
            default=values.get(CONF_DEADBAND, DEFAULT_DEADBAND),
        ): _number(),
        vol.Required(
            CONF_SETPOINT_OFFSET,
            default=values.get(CONF_SETPOINT_OFFSET, DEFAULT_SETPOINT_OFFSET),
        ): _number(),
        vol.Required(
            CONF_MIN_CYCLE_TIME,
            default=values.get(CONF_MIN_CYCLE_TIME, DEFAULT_MIN_CYCLE_TIME),
        ): _integer(),
        vol.Required(
            CONF_SETTLING_TIME,
            default=values.get(CONF_SETTLING_TIME, DEFAULT_SETTLING_TIME),
        ): _integer(),
        vol.Required(
            CONF_TICK_INTERVAL,
            default=values.get(CONF_TICK_INTERVAL, DEFAULT_TICK_INTERVAL),
        ): _integer(1),
        vol.Required(
            CONF_START_MEASUREMENT_DELAY,
            default=values.get(
                CONF_START_MEASUREMENT_DELAY, DEFAULT_START_MEASUREMENT_DELAY
            ),
        ): _integer(),
    }


def _validate_targets(values: dict[str, Any]) -> str | None:
    """Return an error key if heat/cool targets are invalid, else None."""
    heat = values.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET)
    cool = values.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET)
    if heat >= cool:
        return "targets_invalid"
    return None


def _emergency_fields(
    current: dict[str, Any], fan_options: list[str] | None
) -> dict[Any, Any]:
    return {
        vol.Required(
            CONF_ROOM_SENSOR_STALE_MINUTES,
            default=current.get(
                CONF_ROOM_SENSOR_STALE_MINUTES, DEFAULT_ROOM_SENSOR_STALE_MINUTES
            ),
        ): _integer(),
        vol.Required(
            CONF_ROOM_SENSOR_STUCK_HOURS,
            default=current.get(
                CONF_ROOM_SENSOR_STUCK_HOURS, DEFAULT_ROOM_SENSOR_STUCK_HOURS
            ),
        ): _integer(),
        vol.Required(
            CONF_EMERGENCY_ENABLE,
            default=current.get(CONF_EMERGENCY_ENABLE, DEFAULT_EMERGENCY_ENABLE),
        ): bool,
        vol.Optional(
            CONF_OUTDOOR_TEMP_SENSOR,
            description={"suggested_value": current.get(CONF_OUTDOOR_TEMP_SENSOR)},
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
        ),
        vol.Required(
            CONF_EMERGENCY_HEAT_BELOW_OUTDOOR,
            default=current.get(
                CONF_EMERGENCY_HEAT_BELOW_OUTDOOR,
                DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
            ),
        ): vol.Coerce(float),
        vol.Required(
            CONF_EMERGENCY_COOL_ABOVE_OUTDOOR,
            default=current.get(
                CONF_EMERGENCY_COOL_ABOVE_OUTDOOR,
                DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
            ),
        ): vol.Coerce(float),
        vol.Required(
            CONF_EMERGENCY_HEAT_SETPOINT,
            default=current.get(
                CONF_EMERGENCY_HEAT_SETPOINT, DEFAULT_EMERGENCY_HEAT_SETPOINT
            ),
        ): vol.Coerce(float),
        vol.Required(
            CONF_EMERGENCY_COOL_SETPOINT,
            default=current.get(
                CONF_EMERGENCY_COOL_SETPOINT, DEFAULT_EMERGENCY_COOL_SETPOINT
            ),
        ): vol.Coerce(float),
        vol.Required(
            CONF_EMERGENCY_FAN_MODE,
            default=current.get(CONF_EMERGENCY_FAN_MODE, DEFAULT_EMERGENCY_FAN_MODE),
        ): _fan_mode_field(fan_options),
    }


def _saved_fan_mode_values(data: dict[str, Any]) -> list[str]:
    """All fan-mode strings already stored in a config entry / form draft."""
    values = [data.get(mode_key) for _, _, mode_key, _ in FAN_TIER_KEYS]
    values.append(data.get(CONF_EMERGENCY_FAN_MODE))
    values.append(data.get(CONF_FAN_LIMIT_MODE))
    return [v for v in values if isinstance(v, str)]


def _merge_form_values(
    current: dict[str, Any],
    user_input: dict[str, Any],
    optional_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Merge a partial options page, removing optional values the user cleared."""
    merged = {**current, **user_input}
    for key in optional_keys:
        if key not in user_input:
            merged.pop(key, None)
    return merged


class ClimateOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Optimizer."""

    VERSION = CONFIG_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            target_error = _validate_targets(user_input)
            if target_error:
                errors["base"] = target_error
            else:
                unique = (
                    f"{user_input[CONF_DOWNSTREAM_CLIMATE]}::"
                    f"{user_input[CONF_SOURCE_TEMP_SENSOR]}"
                )
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=user_input
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=(user_input or {}).get(CONF_NAME, "")
                ): str,
                vol.Required(
                    CONF_SOURCE_TEMP_SENSOR,
                    default=(user_input or {}).get(CONF_SOURCE_TEMP_SENSOR),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="temperature"
                    )
                ),
                vol.Optional(
                    CONF_SOURCE_HUMIDITY_SENSOR,
                    description={
                        "suggested_value": (user_input or {}).get(
                            CONF_SOURCE_HUMIDITY_SENSOR
                        )
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class="humidity"
                    )
                ),
                vol.Required(
                    CONF_DOWNSTREAM_CLIMATE,
                    default=(user_input or {}).get(CONF_DOWNSTREAM_CLIMATE),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                vol.Optional(
                    CONF_AREA_ID,
                    description={
                        "suggested_value": (user_input or {}).get(CONF_AREA_ID)
                    },
                ): selector.AreaSelector(),
                **_target_fields(user_input or {}),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return ClimateOptimizerOptionsFlow()


class ClimateOptimizerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for a Climate Optimizer virtual device."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show links to the targets and advanced options pages."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["targets", "fan_limit", "advanced"],
        )

    async def async_step_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Targets and area."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            target_error = _validate_targets(user_input)
            if target_error:
                errors["base"] = target_error
            else:
                merged = _merge_form_values(
                    current, user_input, optional_keys=(CONF_AREA_ID,)
                )
                return self.async_create_entry(title="", data=merged)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_AREA_ID,
                    description={"suggested_value": current.get(CONF_AREA_ID)},
                ): selector.AreaSelector(),
                **_target_fields(current),
            }
        )
        return self.async_show_form(
            step_id="targets",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Advanced settings: timing, fan tiers, emergency."""
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            merged = _merge_form_values(
                current, user_input, optional_keys=(CONF_OUTDOOR_TEMP_SENSOR,)
            )
            return self.async_create_entry(title="", data=merged)

        fan_options = _fan_mode_options(
            self.hass,
            current.get(CONF_DOWNSTREAM_CLIMATE),
            _saved_fan_mode_values(current),
        )
        schema = vol.Schema(
            {
                **_advanced_control_fields(current),
                **_fan_tier_fields(current, fan_options),
                **_emergency_fields(current, fan_options),
            }
        )
        return self.async_show_form(
            step_id="advanced",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_fan_limit(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Set or clear a temporary maximum fan mode."""
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            merged = {**current}
            max_hours = float(user_input[CONF_FAN_LIMIT_MAX_HOURS])
            hours = min(float(user_input[CONF_FAN_LIMIT_HOURS]), max_hours)
            merged[CONF_FAN_LIMIT_MAX_HOURS] = max_hours
            if hours > 0:
                merged[CONF_FAN_LIMIT_HOURS] = hours
                merged[CONF_FAN_LIMIT_MODE] = user_input[CONF_FAN_LIMIT_MODE]
                merged[CONF_FAN_LIMIT_UNTIL] = (
                    dt_util.utcnow() + timedelta(hours=hours)
                ).isoformat()
            else:
                merged.pop(CONF_FAN_LIMIT_HOURS, None)
                merged.pop(CONF_FAN_LIMIT_MODE, None)
                merged.pop(CONF_FAN_LIMIT_UNTIL, None)
            return self.async_create_entry(title="", data=merged)

        fan_options = _fan_mode_options(
            self.hass,
            current.get(CONF_DOWNSTREAM_CLIMATE),
            _saved_fan_mode_values(current),
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FAN_LIMIT_MODE,
                    default=current.get(CONF_FAN_LIMIT_MODE, "low"),
                ): _fan_mode_field(fan_options),
                vol.Required(
                    CONF_FAN_LIMIT_HOURS,
                    default=current.get(CONF_FAN_LIMIT_HOURS, DEFAULT_FAN_LIMIT_HOURS),
                ): _number(),
                vol.Required(
                    CONF_FAN_LIMIT_MAX_HOURS,
                    default=current.get(
                        CONF_FAN_LIMIT_MAX_HOURS, DEFAULT_FAN_LIMIT_MAX_HOURS
                    ),
                ): _number(0.5),
            }
        )
        return self.async_show_form(
            step_id="fan_limit",
            data_schema=schema,
        )
