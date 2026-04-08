"""Constants for the Climate Optimizer integration."""
from __future__ import annotations

DOMAIN = "climate_optimizer"

CONF_SOURCE_TEMP_SENSOR = "source_temp_sensor"
CONF_SOURCE_HUMIDITY_SENSOR = "source_humidity_sensor"
CONF_DOWNSTREAM_CLIMATE = "downstream_climate"
CONF_HEAT_TARGET = "heat_target"
CONF_COOL_TARGET = "cool_target"
CONF_DEADBAND = "deadband"
CONF_SETPOINT_OFFSET = "setpoint_offset"
CONF_MIN_CYCLE_TIME = "min_cycle_time"
CONF_TICK_INTERVAL = "tick_interval"
CONF_FAN_TIERS = "fan_tiers"

CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_EMERGENCY_ENABLE = "emergency_enable"
CONF_EMERGENCY_HEAT_BELOW_OUTDOOR = "emergency_heat_below_outdoor"
CONF_EMERGENCY_COOL_ABOVE_OUTDOOR = "emergency_cool_above_outdoor"
CONF_EMERGENCY_HEAT_SETPOINT = "emergency_heat_setpoint"
CONF_EMERGENCY_COOL_SETPOINT = "emergency_cool_setpoint"
CONF_EMERGENCY_FAN_MODE = "emergency_fan_mode"

CONF_AREA_ID = "area_id"

# Fan tiers are stored in the entry both as the legacy "fan_tiers" list AND as
# four flat (error, mode) pairs so they can be edited in the UI config flow.
CONF_FAN_TIER_1_ERROR = "fan_tier_1_error"
CONF_FAN_TIER_1_MODE = "fan_tier_1_mode"
CONF_FAN_TIER_2_ERROR = "fan_tier_2_error"
CONF_FAN_TIER_2_MODE = "fan_tier_2_mode"
CONF_FAN_TIER_3_ERROR = "fan_tier_3_error"
CONF_FAN_TIER_3_MODE = "fan_tier_3_mode"
CONF_FAN_TIER_4_ERROR = "fan_tier_4_error"
CONF_FAN_TIER_4_MODE = "fan_tier_4_mode"

DEFAULT_FAN_TIER_1_ERROR = 1.0
DEFAULT_FAN_TIER_1_MODE = "low"
DEFAULT_FAN_TIER_2_ERROR = 3.0
DEFAULT_FAN_TIER_2_MODE = "medium"
DEFAULT_FAN_TIER_3_ERROR = 5.0
DEFAULT_FAN_TIER_3_MODE = "high"
DEFAULT_FAN_TIER_4_ERROR = 999.0
DEFAULT_FAN_TIER_4_MODE = "turbo"

DEFAULT_HEAT_TARGET = 65.0
DEFAULT_COOL_TARGET = 70.0
DEFAULT_DEADBAND = 0.5
DEFAULT_SETPOINT_OFFSET = 4.0
DEFAULT_MIN_CYCLE_TIME = 300  # seconds
DEFAULT_TICK_INTERVAL = 30  # seconds

DEFAULT_EMERGENCY_ENABLE = True
DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR = 35.0  # pipes stay safe
DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR = 95.0
DEFAULT_EMERGENCY_HEAT_SETPOINT = 62.0
DEFAULT_EMERGENCY_COOL_SETPOINT = 80.0
DEFAULT_EMERGENCY_FAN_MODE = "low"

# Fan tiers map "how far out of the target band are we" to a downstream fan_mode
# name. Tiers are evaluated in ascending max_error order; the first one whose
# max_error >= current error wins. Keep this flexible so arbitrary fan mode
# strings (for future unit types) work without code changes.
DEFAULT_FAN_TIERS = [
    {"max_error": 1.0, "fan_mode": "low"},
    {"max_error": 3.0, "fan_mode": "medium"},
    {"max_error": 5.0, "fan_mode": "high"},
    {"max_error": 999.0, "fan_mode": "turbo"},
]
