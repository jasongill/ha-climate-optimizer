"""Config flow for Climate Optimizer."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

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
    CONF_SETPOINT_OFFSET,
    CONF_SOURCE_HUMIDITY_SENSOR,
    CONF_SOURCE_TEMP_SENSOR,
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
    DEFAULT_HEAT_TARGET,
    DEFAULT_MIN_CYCLE_TIME,
    DEFAULT_SETPOINT_OFFSET,
    DEFAULT_TICK_INTERVAL,
    DOMAIN,
    FAN_TIER_KEYS,
)


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
    fan_modes = state.attributes.get("fan_modes")
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
        fields[
            vol.Required(err_key, default=current.get(err_key, err_default))
        ] = vol.Coerce(float)
        fields[
            vol.Required(mode_key, default=current.get(mode_key, mode_default))
        ] = _fan_mode_field(fan_options)
    return fields


def _emergency_fields(
    current: dict[str, Any], fan_options: list[str] | None
) -> dict[Any, Any]:
    return {
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


def _basics_schema(defaults: dict[str, Any]) -> vol.Schema:
    """First config step: entities, area, targets, timing."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(
                CONF_SOURCE_TEMP_SENSOR,
                default=defaults.get(CONF_SOURCE_TEMP_SENSOR),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature"
                )
            ),
            vol.Optional(
                CONF_SOURCE_HUMIDITY_SENSOR,
                description={
                    "suggested_value": defaults.get(CONF_SOURCE_HUMIDITY_SENSOR)
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="humidity"
                )
            ),
            vol.Required(
                CONF_DOWNSTREAM_CLIMATE,
                default=defaults.get(CONF_DOWNSTREAM_CLIMATE),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate")
            ),
            vol.Optional(
                CONF_AREA_ID,
                description={"suggested_value": defaults.get(CONF_AREA_ID)},
            ): selector.AreaSelector(),
            vol.Required(
                CONF_HEAT_TARGET,
                default=defaults.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_COOL_TARGET,
                default=defaults.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_DEADBAND, default=defaults.get(CONF_DEADBAND, DEFAULT_DEADBAND)
            ): vol.Coerce(float),
            vol.Required(
                CONF_SETPOINT_OFFSET,
                default=defaults.get(CONF_SETPOINT_OFFSET, DEFAULT_SETPOINT_OFFSET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MIN_CYCLE_TIME,
                default=defaults.get(CONF_MIN_CYCLE_TIME, DEFAULT_MIN_CYCLE_TIME),
            ): vol.Coerce(int),
            vol.Required(
                CONF_TICK_INTERVAL,
                default=defaults.get(CONF_TICK_INTERVAL, DEFAULT_TICK_INTERVAL),
            ): vol.Coerce(int),
        }
    )


def _fan_tiers_schema(
    defaults: dict[str, Any], fan_options: list[str] | None
) -> vol.Schema:
    """Second config step: fan tiers + emergency."""
    return vol.Schema(
        {
            **_fan_tier_fields(defaults, fan_options),
            **_emergency_fields(defaults, fan_options),
        }
    )


def _options_schema(
    current: dict[str, Any], fan_options: list[str] | None
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_AREA_ID,
                description={"suggested_value": current.get(CONF_AREA_ID)},
            ): selector.AreaSelector(),
            vol.Required(
                CONF_HEAT_TARGET,
                default=current.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_COOL_TARGET,
                default=current.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_DEADBAND,
                default=current.get(CONF_DEADBAND, DEFAULT_DEADBAND),
            ): vol.Coerce(float),
            vol.Required(
                CONF_SETPOINT_OFFSET,
                default=current.get(CONF_SETPOINT_OFFSET, DEFAULT_SETPOINT_OFFSET),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MIN_CYCLE_TIME,
                default=current.get(CONF_MIN_CYCLE_TIME, DEFAULT_MIN_CYCLE_TIME),
            ): vol.Coerce(int),
            vol.Required(
                CONF_TICK_INTERVAL,
                default=current.get(CONF_TICK_INTERVAL, DEFAULT_TICK_INTERVAL),
            ): vol.Coerce(int),
            **_fan_tier_fields(current, fan_options),
            **_emergency_fields(current, fan_options),
        }
    )


def _saved_fan_mode_values(data: dict[str, Any]) -> list[str]:
    """All fan-mode strings already stored in a config entry / form draft."""
    values = [data.get(mode_key) for _, _, mode_key, _ in FAN_TIER_KEYS]
    values.append(data.get(CONF_EMERGENCY_FAN_MODE))
    return [v for v in values if isinstance(v, str)]


class ClimateOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Optimizer."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        self._basics: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_HEAT_TARGET] >= user_input[CONF_COOL_TARGET]:
                errors["base"] = "targets_invalid"
            else:
                unique = (
                    f"{user_input[CONF_DOWNSTREAM_CLIMATE]}::"
                    f"{user_input[CONF_SOURCE_TEMP_SENSOR]}"
                )
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                self._basics = user_input
                return await self.async_step_fan_tiers()

        return self.async_show_form(
            step_id="user",
            data_schema=_basics_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_fan_tiers(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            data = {**self._basics, **user_input}
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        fan_options = _fan_mode_options(
            self.hass,
            self._basics.get(CONF_DOWNSTREAM_CLIMATE),
            _saved_fan_mode_values(self._basics),
        )
        return self.async_show_form(
            step_id="fan_tiers",
            data_schema=_fan_tiers_schema(self._basics, fan_options),
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
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            if user_input[CONF_HEAT_TARGET] >= user_input[CONF_COOL_TARGET]:
                errors["base"] = "targets_invalid"
            else:
                return self.async_create_entry(title="", data=user_input)

        fan_options = _fan_mode_options(
            self.hass,
            current.get(CONF_DOWNSTREAM_CLIMATE),
            _saved_fan_mode_values(current),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current, fan_options),
            errors=errors,
        )
