"""Constants for the Climate Optimizer integration."""
from __future__ import annotations

DOMAIN = "climate_optimizer"

CONFIG_VERSION = 2

CONF_SOURCE_TEMP_SENSOR = "source_temp_sensor"
CONF_SOURCE_HUMIDITY_SENSOR = "source_humidity_sensor"
CONF_DOWNSTREAM_CLIMATE = "downstream_climate"
CONF_AREA_ID = "area_id"

CONF_HEAT_TARGET = "heat_target"
CONF_COOL_TARGET = "cool_target"
CONF_DEADBAND = "deadband"
CONF_SETPOINT_OFFSET = "setpoint_offset"
CONF_MIN_CYCLE_TIME = "min_cycle_time"
CONF_TICK_INTERVAL = "tick_interval"
CONF_START_MEASUREMENT_DELAY = "start_measurement_delay"

# Fan tiers map "how far out of the target band are we" to a downstream
# fan_mode name. Stored as four flat (error, mode) pairs so they can be
# edited directly in the config flow UI. The entity sorts them by max_error
# and picks the first tier whose threshold is >= current error.
CONF_FAN_TIER_1_ERROR = "fan_tier_1_error"
CONF_FAN_TIER_1_MODE = "fan_tier_1_mode"
CONF_FAN_TIER_2_ERROR = "fan_tier_2_error"
CONF_FAN_TIER_2_MODE = "fan_tier_2_mode"
CONF_FAN_TIER_3_ERROR = "fan_tier_3_error"
CONF_FAN_TIER_3_MODE = "fan_tier_3_mode"
CONF_FAN_TIER_4_ERROR = "fan_tier_4_error"
CONF_FAN_TIER_4_MODE = "fan_tier_4_mode"

CONF_ROOM_SENSOR_STALE_MINUTES = "room_sensor_stale_minutes"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_EMERGENCY_ENABLE = "emergency_enable"
CONF_EMERGENCY_HEAT_BELOW_OUTDOOR = "emergency_heat_below_outdoor"
CONF_EMERGENCY_COOL_ABOVE_OUTDOOR = "emergency_cool_above_outdoor"
CONF_EMERGENCY_HEAT_SETPOINT = "emergency_heat_setpoint"
CONF_EMERGENCY_COOL_SETPOINT = "emergency_cool_setpoint"
CONF_EMERGENCY_FAN_MODE = "emergency_fan_mode"

DEFAULT_HEAT_TARGET = 62.0
DEFAULT_COOL_TARGET = 74.0
DEFAULT_DEADBAND = 0.5
DEFAULT_SETPOINT_OFFSET = 4.0
DEFAULT_MIN_CYCLE_TIME = 300  # seconds
DEFAULT_TICK_INTERVAL = 30  # seconds
DEFAULT_START_MEASUREMENT_DELAY = 120  # seconds

DEFAULT_FAN_TIER_1_ERROR = 1.0
DEFAULT_FAN_TIER_1_MODE = "low"
DEFAULT_FAN_TIER_2_ERROR = 3.0
DEFAULT_FAN_TIER_2_MODE = "medium"
DEFAULT_FAN_TIER_3_ERROR = 5.0
DEFAULT_FAN_TIER_3_MODE = "high"
DEFAULT_FAN_TIER_4_ERROR = 999.0
DEFAULT_FAN_TIER_4_MODE = "turbo"

DEFAULT_ROOM_SENSOR_STALE_MINUTES = 60  # 1 hour

DEFAULT_EMERGENCY_ENABLE = True
DEFAULT_EMERGENCY_HEAT_BELOW_OUTDOOR = 40.0
DEFAULT_EMERGENCY_COOL_ABOVE_OUTDOOR = 90.0
DEFAULT_EMERGENCY_HEAT_SETPOINT = 62.0
DEFAULT_EMERGENCY_COOL_SETPOINT = 80.0
DEFAULT_EMERGENCY_FAN_MODE = "high"

# Ordered list used to iterate over all fan tier fields at once.
FAN_TIER_KEYS: list[tuple[str, float, str, str]] = [
    (CONF_FAN_TIER_1_ERROR, DEFAULT_FAN_TIER_1_ERROR, CONF_FAN_TIER_1_MODE, DEFAULT_FAN_TIER_1_MODE),
    (CONF_FAN_TIER_2_ERROR, DEFAULT_FAN_TIER_2_ERROR, CONF_FAN_TIER_2_MODE, DEFAULT_FAN_TIER_2_MODE),
    (CONF_FAN_TIER_3_ERROR, DEFAULT_FAN_TIER_3_ERROR, CONF_FAN_TIER_3_MODE, DEFAULT_FAN_TIER_3_MODE),
    (CONF_FAN_TIER_4_ERROR, DEFAULT_FAN_TIER_4_ERROR, CONF_FAN_TIER_4_MODE, DEFAULT_FAN_TIER_4_MODE),
]
