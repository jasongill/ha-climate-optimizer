# Climate Optimizer

A Home Assistant custom integration that wraps a "dumb" climate device (such as a mini split with an unreliable or poorly located internal sensor) with a **virtual climate entity** driven by an external temperature/humidity sensor in the same room.

Each virtual climate device pairs one room sensor with one downstream climate entity and runs its own control loop, so you can get tight room-level behavior out of equipment that would otherwise let temperature drift or idle its indoor fan 24/7.

## v0.9.2 — recovery from unconfirmed commands

- Lost mode, temperature, and fan commands retry after 30 seconds, then 60,
  120, 240, and at most 300 seconds between attempts. Retries run on the next
  control event or configured timer tick after that delay; confirmed commands
  are not repeated. Service calls time out after 30 seconds.
- A downstream disconnect clears stale pending commands on recovery, including
  brief disconnects. The controller reconciles the current demand automatically.
- Off commands also retry, including when the virtual thermostat is switched off
  while the split is disconnected. Manual mode changes serialize with the control
  loop so an in-flight cooling pass cannot overwrite an off request.
- Status distinguishes requested heating/cooling from the downstream's confirmed
  mode. `last_sent` and `command_attempts` expose outstanding requests; retries and
  service failures are logged.
- Thermal learning skips unconfirmed operation and breaks observations across
  disconnects or missing room-sensor data, preserving previously learned values.

Confirmation means the downstream HA entity reports the requested mode; it does
not independently verify compressor operation. Existing room-sensor safeguards,
comfort targets, and minimum-cycle rules continue to apply.

## What it does

For every virtual climate device you create, the integration:

- Reads a **room temperature sensor** you pick (and optionally a humidity sensor for display).
- Watches a **target range** with a configurable hysteresis deadband.
- Drives a **downstream climate entity** (the real mini split) to hit that range.
- Picks a **fan mode** based on how far the room is from the target band.
- Turns the downstream unit **fully off** once the room is back in range — no idling fan.
- Respects a **minimum cycle time** between transitions to protect the compressor.
- Falls back to a conservative **emergency mode** if the room sensor goes offline, optionally gated by an outdoor temperature sensor, to protect the room (and your pipes) until the sensor comes back.

The virtual entity exposes a `decision_reason` attribute so you can see, at a glance, why it is doing whatever it is doing.

## How the control loop works

The state machine uses hysteresis, temperature trajectory, and an explicit post-cycle settling period:

- **Start cooling** when the room climbs to `cool_target + deadband`. Command the downstream unit to `cool` with a setpoint pushed `setpoint_offset` degrees **below** the cool target, so the unit actually runs instead of thinking it is already at temperature.
- **Start heating** when the room drops to `heat_target - deadband`. Mirror image: command `heat` with a setpoint pushed `setpoint_offset` degrees **above** the heat target.
- **Ease off near target** by shrinking the downstream setpoint offset toward 2°F. This lets an inverter run longer at lower output instead of repeatedly demanding maximum capacity.
- **Stop conservatively** at the filtered room target, or slightly early only when a high-confidence five-minute projection agrees with the live trend. Afterward, observe room mixing only while demand remains satisfied; crossing a normal start threshold ends the mixing window immediately.
- On every tick, the commanded fan mode is re-evaluated based on the current error from the target band and the configured fan tiers.

Downstream commands are de-duplicated — the integration only resends mode/setpoint/fan changes when they actually differ from the downstream entity's current state.

## Low-sawtooth control

Remote room sensors react more slowly than air at a wall-mounted mini split. The integration keeps a de-duplicated 20-minute sample buffer, filters the three most recent readings with a median, and fits a temperature slope over the last 10 minutes. A five-minute projection allows cooling or heating to stop before sensor lag carries the room through the target.

The configured downstream offset is a maximum. Within 1°F of target, the controller uses at most a 2°F offset; from 1–3°F error it interpolates smoothly toward the configured maximum. Fan mode continues to follow the configured tiers, but the controller never automatically boosts past the natural tier.

The mini split's own temperature bias is retained as a diagnostic attribute but does not increase demand. This avoids extreme commands when a wall-unit sensor is stale or reads in the cold/warm discharge plume.

### Continuous learning

Each virtual device continuously learns bounded temperature-change rates separately for heating and cooling, for every fan mode it actually uses. When an outdoor sensor is configured, it also keeps coarse 10°F outdoor-temperature buckets so a mild spring day does not overwrite peak-summer behavior. The learner blends those estimates with the live room trend and learns the remaining temperature drift after shutdown.

Learning persists in the entity's restored state across Home Assistant restarts. Confidence rises gradually over repeated observations. A large temperature discontinuity while the equipment is idle is treated as a likely sensor move: confidence is reduced and adaptation temporarily speeds up. Learned values affect predictive stopping only—they cannot select a higher fan tier, exceed a temporary fan limit, or push a more aggressive downstream setpoint.

### Visibility
Control estimates and diagnostics are exposed as entity attributes:

| Attribute | Meaning |
| --- | --- |
| `decision_reason` | Plain-language description of the current control decision |
| `filtered_room_temperature` | Median-filtered room temperature used for control |
| `room_temperature_slope_per_minute` | Least-squares slope over recent readings |
| `projected_room_temperature_5m` | Five-minute room-temperature projection |
| `thermal_learning_confidence` | Confidence in the active mode/fan/outdoor model (0–1) |
| `thermal_learning` | Persistent learned rates, post-stop drift, sample counts, and detected sensor moves |
| `settling_remaining_seconds` | Time before a post-cycle restart is allowed |
| `downstream_sensor_bias` | Smoothed delta between minisplit sensor and room sensor |
| `downstream_sensor_stale` | True when the minisplit sensor has frozen |
| `downstream_sensor_age_s` | Seconds since the minisplit sensor last reported a new value |
| `recent_heat_starts` / `recent_cool_starts` | Recent cycle start timestamps for diagnostics |

## Configuration

### Setup (initial)

When you add the integration, you only need to provide the essentials. Everything else uses conservative defaults.

| Field | Meaning | Default |
| --- | --- | --- |
| Virtual Climate Device Name | Name for the virtual climate entity | — |
| Room temperature sensor | Temperature sensor to read | — |
| Room humidity sensor | Optional, used for display | — |
| Downstream climate entity | The real mini split to command | — |
| Area | Optional area assignment for the device | — |
| Heat target | Below this, start heating (°F). Adjustable later from the thermostat card; persists across restarts. | 62 |
| Cool target | Above this, start cooling (°F). Adjustable later from the thermostat card; persists across restarts. | 74 |

### Options (Configure → Targets & Area)

After setup, use **Configure** on the integration entry to adjust heat/cool targets and area assignment.

### Options (Configure → Advanced Settings)

For power users. Most installs should begin with the defaults and tune from observed room history.

| Field | Meaning | Default |
| --- | --- | --- |
| Deadband | Hysteresis before starting a cycle (°F) | 0.5 |
| Setpoint offset | Degrees past the target to push the downstream setpoint | 4 |
| Minimum cycle time | Seconds to wait between transitions | 300 |
| Maximum room mixing time | Maximum seconds to observe room mixing after shutdown while demand remains satisfied | 120 |
| Control loop interval | Safety-net tick in addition to sensor updates (s) | 30 |
| Start measurement delay | Seconds to ignore stop threshold after cycle start (avoids sensor blowby false stops) | 120 |
| Room sensor stale minutes | If the room sensor hasn't updated in this many minutes, treat it as lost and trigger emergency mode (0 to disable) | 60 |
| Fan tiers (4 tiers) | Maps error-from-target to a fan mode name. Defaults: ≤1°F → `low`, ≤3°F → `medium`, ≤5°F → `high`, everything else → `turbo`. Fan mode names are free-form strings, so any downstream unit's naming works. | See left |
| Emergency fallback | When the room sensor goes offline or stale: optionally force heat/cool based on outdoor temp to protect the room. | Enabled |
| Outdoor temp sensor | For seasonal learning and emergency fallback decisions | — |
| Emergency thresholds | Force heat below 40°F outdoor, force cool above 90°F outdoor | 40 / 90 |
| Emergency setpoints | Conservative fixed setpoints during emergency | Heat 62°F, Cool 80°F |
| Emergency fan mode | Fan mode during emergency | `high` |

Emergency mode respects the virtual entity's current `hvac_mode` — if you've set it to `heat_only`, it won't emergency-cool you.

### Temporary fan limit

Each virtual device provides **Maximum Fan Speed** and **Fan Limit Duration** entities for dashboards and automations. Set the duration, then select a maximum speed such as `low`; selecting `Disabled` clears the cap. The default duration is four hours.

The same controls remain available under **Configure → Temporary Fan Limit**, along with the maximum duration exposed by the dashboard number entity (24 hours by default). Dashboard changes apply live without restarting the climate control loop. The limit survives Home Assistant restarts and expires automatically. Emergency fallback is intentionally allowed to exceed this cap.

## Control source

The virtual climate entity is intended to be the **only** thing driving the downstream unit. Any changes made directly to the downstream climate entity will be overwritten on the next tick. Treat the downstream entity as an implementation detail and drive everything through the virtual entity.

## Installation

This integration is distributed via [HACS](https://hacs.xyz/) as a custom repository:

1. In Home Assistant, open **HACS → Integrations**.
2. From the menu, choose **Custom repositories**.
3. Add `https://github.com/jasongill/ha-climate-optimizer` with category **Integration**.
4. Install **Climate Optimizer** from the HACS list and restart Home Assistant.
5. Go to **Settings → Devices & Services → Add Integration** and search for **Climate Optimizer**.
6. Create one virtual climate device per room/mini split pair.

## Requirements

- Home Assistant **2026.3** or newer.
- A downstream `climate` entity that supports `heat`, `cool`, `off`, a `target_temperature`, and one or more `fan_modes`.
- A `sensor` entity reporting room temperature (device_class `temperature`). Humidity and outdoor temperature sensors are optional.
