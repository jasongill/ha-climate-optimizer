"""Config flow for Climate Optimizer."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
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
    CONF_FAN_TIER_1_ERROR,
    CONF_FAN_TIER_1_MODE,
    CONF_FAN_TIER_2_ERROR,
    CONF_FAN_TIER_2_MODE,
    CONF_FAN_TIER_3_ERROR,
    CONF_FAN_TIER_3_MODE,
    CONF_FAN_TIER_4_ERROR,
    CONF_FAN_TIER_4_MODE,
    CONF_FAN_TIERS,
    CONF_HEAT_TARGET,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_SETPOINT_OFFSET,
    CONF_SOURCE_HUMIDITY_SENSOR,
    CONF_SOURCE_TEMP_SENSOR,
    CONF_TICK_INTERVAL,
    DEFAULT_COOL_TARGET,
    DEFAULT_DEADBAND,
    DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR,
    DEFAULT_EMERGENCY_COOL_SETPOINT,
    DEFAULT_EMERGENCY_ENABLE,
    DEFAULT_EMERGENCY_FAN_MODE,
    DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR,
    DEFAULT_EMERGENCY_HEAT_SETPOINT,
    DEFAULT_FAN_TIER_1_ERROR,
    DEFAULT_FAN_TIER_1_MODE,
    DEFAULT_FAN_TIER_2_ERROR,
    DEFAULT_FAN_TIER_2_MODE,
    DEFAULT_FAN_TIER_3_ERROR,
    DEFAULT_FAN_TIER_3_MODE,
    DEFAULT_FAN_TIER_4_ERROR,
    DEFAULT_FAN_TIER_4_MODE,
    DEFAULT_HEAT_TARGET,
    DEFAULT_MIN_CYCLE_TIME,
    DEFAULT_SETPOINT_OFFSET,
    DEFAULT_TICK_INTERVAL,
    DOMAIN,
)


_FAN_TIER_DEFAULTS = [
    (CONF_FAN_TIER_1_ERROR, DEFAULT_FAN_TIER_1_ERROR, CONF_FAN_TIER_1_MODE, DEFAULT_FAN_TIER_1_MODE),
    (CONF_FAN_TIER_2_ERROR, DEFAULT_FAN_TIER_2_ERROR, CONF_FAN_TIER_2_MODE, DEFAULT_FAN_TIER_2_MODE),
    (CONF_FAN_TIER_3_ERROR, DEFAULT_FAN_TIER_3_ERROR, CONF_FAN_TIER_3_MODE, DEFAULT_FAN_TIER_3_MODE),
    (CONF_FAN_TIER_4_ERROR, DEFAULT_FAN_TIER_4_ERROR, CONF_FAN_TIER_4_MODE, DEFAULT_FAN_TIER_4_MODE),
]


def _fan_tier_fields(current: dict[str, Any]) -> dict[Any, Any]:
    fields: dict[Any, Any] = {}
    for err_key, err_default, mode_key, mode_default in _FAN_TIER_DEFAULTS:
        fields[
            vol.Required(err_key, default=current.get(err_key, err_default))
        ] = vol.Coerce(float)
        fields[
            vol.Required(mode_key, default=current.get(mode_key, mode_default))
        ] = str
    return fields


def _build_fan_tiers_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Reassemble the flat tier fields into the list form used by the entity."""
    tiers: list[dict[str, Any]] = []
    for err_key, err_default, mode_key, mode_default in _FAN_TIER_DEFAULTS:
        mode = str(cfg.get(mode_key, mode_default)).strip()
        if not mode:
            continue
        tiers.append(
            {
                "max_error": float(cfg.get(err_key, err_default)),
                "fan_mode": mode,
            }
        )
    return tiers


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
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
            vol.Optional(
                CONF_AREA_ID,
                description={"suggested_value": defaults.get(CONF_AREA_ID)},
            ): selector.AreaSelector(),
            **_fan_tier_fields(defaults),
            **_emergency_fields(defaults),
        }
    )


def _emergency_fields(current: dict[str, Any]) -> dict[Any, Any]:
    """Fields for the emergency-fallback section, shared by setup + options flows."""
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_EMERGENCY_ENABLE,
            default=current.get(CONF_EMERGENCY_ENABLE, DEFAULT_EMERGENCY_ENABLE),
        ): bool,
        vol.Optional(
            CONF_OUTDOOR_TEMP_SENSOR,
            description={
                "suggested_value": current.get(CONF_OUTDOOR_TEMP_SENSOR)
            },
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class="temperature"
            )
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
            default=current.get(
                CONF_EMERGENCY_FAN_MODE, DEFAULT_EMERGENCY_FAN_MODE
            ),
        ): str,
    }
    return fields


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
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
            vol.Optional(
                CONF_AREA_ID,
                description={"suggested_value": current.get(CONF_AREA_ID)},
            ): selector.AreaSelector(),
            **_fan_tier_fields(current),
            **_emergency_fields(current),
        }
    )


class ClimateOptimizerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Climate Optimizer."""

    VERSION = 1

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
                data = {
                    **user_input,
                    CONF_FAN_TIERS: _build_fan_tiers_list(user_input),
                }
                return self.async_create_entry(
                    title=user_input[CONF_NAME], data=data
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return ClimateOptimizerOptionsFlow(config_entry)


class ClimateOptimizerOptionsFlow(config_entries.OptionsFlow):
    """Handle options for a Climate Optimizer zone."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_HEAT_TARGET] >= user_input[CONF_COOL_TARGET]:
                errors["base"] = "targets_invalid"
            else:
                user_input[CONF_FAN_TIERS] = _build_fan_tiers_list(user_input)
                return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(current), errors=errors
        )
