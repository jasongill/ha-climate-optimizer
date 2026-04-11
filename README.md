# Climate Optimizer

A Home Assistant custom integration that wraps a "dumb" climate device (such as a mini split with an unreliable or poorly located internal sensor) with a **virtual climate entity** driven by an external temperature/humidity sensor in the same room.

Each virtual climate device pairs one room sensor with one downstream climate entity and runs its own control loop, so you can get tight room-level behavior out of equipment that would otherwise let temperature drift or idle its indoor fan 24/7.

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

The state machine is intentionally simple and uses asymmetric hysteresis to avoid short cycling:

- **Start cooling** when the room climbs to `cool_target + deadband`. Command the downstream unit to `cool` with a setpoint pushed `setpoint_offset` degrees **below** the cool target, so the unit actually runs instead of thinking it is already at temperature.
- **Start heating** when the room drops to `heat_target - deadband`. Mirror image: command `heat` with a setpoint pushed `setpoint_offset` degrees **above** the heat target.
- **Stop** (turn the downstream unit fully off) when the room reaches the target (plus any adaptive overshoot — see below), then wait `min_cycle_time` before another transition is allowed.
- On every tick, the commanded fan mode is re-evaluated based on the current error from the target band and the configured fan tiers.

Downstream commands are de-duplicated — the integration only resends mode/setpoint/fan changes when they actually differ from the downstream entity's current state.

## Adaptive control

The basic state machine is fine for well-behaved rooms, but real-world installs are messy: leaky rooms short-cycle, the minisplit's own sensor lies, and inverters under-modulate when their perceived setpoint delta is small. Four learning mechanisms run on top of the base loop to handle this automatically — no user tuning required.

### Adaptive overshoot (per zone, persists across restarts)
When a heat or cool cycle starts within `30 min` of the previous start of the same mode, the integration treats this as short-cycling and lengthens the *stop* threshold for that mode by `0.5°F`, capped at `2°F`. So a leaky room that would otherwise stop heat at exactly `62°F` will end up running to `62.5°F`, then `63°F`, etc., until cycles stretch to a comfortable length. The overshoot decays asymmetrically (`0.25°F` per long cycle) so learning persists overnight and only fades when conditions clearly improve.

### Downstream sensor bias compensation (persists across restarts)
The integration reads the minisplit's own `current_temperature` attribute and tracks the smoothed difference between *its* sensor and the *room* sensor. If the unit thinks it's `3°F` warmer than reality (very common when it's mounted high on the wall), the pushed setpoint is automatically lifted by `3°F` to restore the inverter's perceived gap. Compensation only applies in the direction that makes the unit work *harder* — never softer — and is capped at `5°F`.

Many minisplit platforms (aux, midea) refresh `current_temperature` only on a write, so the value can be hours stale. The integration detects this: if the downstream value hasn't changed for `10 min` *while* the room sensor has clearly moved, the bias EMA stops updating until the downstream finally refreshes. The previously-learned bias still drives compensation in the meantime — better than ignoring it.

### Setpoint boost (within-cycle, free)
Every `5 min` while a cycle is running, progress is sampled. If the room error has shrunk by less than `0.5°F` over the interval (or has gotten worse), the pushed setpoint is bumped another `1°F` further from target, up to `4°F` extra. Inverters scale compressor speed with the perceived delta, so this directly increases BTU/min at no comfort cost.

### Fan boost (within-cycle, last resort)
Once setpoint boost is exhausted and progress is *still* stalled, the chosen fan tier is shifted up one slot per stall window. This is the only adaptive lever that costs noise, so it's intentionally last in the escalation order.

Both within-cycle boosts reset on every new cycle.

### Visibility
Every adaptive value is exposed as an entity attribute so you can see exactly what the system has learned and why it's doing what it's doing:

| Attribute | Meaning |
| --- | --- |
| `decision_reason` | Plain-language description of the current tick's decision, including any active boosts and bias |
| `adaptive_heat_overshoot` / `adaptive_cool_overshoot` | Current learned overshoot in °F per mode |
| `downstream_sensor_bias` | Smoothed delta between minisplit sensor and room sensor |
| `downstream_sensor_stale` | True when the minisplit sensor has frozen |
| `downstream_sensor_age_s` | Seconds since the minisplit sensor last reported a new value |
| `setpoint_boost` | Current within-cycle setpoint push (resets per cycle) |
| `fan_boost` | Current within-cycle fan-tier escalation (resets per cycle) |
| `recent_heat_starts` / `recent_cool_starts` | Recent cycle start timestamps used by the overshoot logic |

## Configuration

### Setup (initial)

When you add the integration, you only need to provide the essentials. Everything else uses smart defaults and the adaptive control system handles tuning automatically.

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

For power users. Most installs won't need to touch these — the adaptive systems handle tuning.

| Field | Meaning | Default |
| --- | --- | --- |
| Deadband | Hysteresis before starting a cycle (°F) | 0.5 |
| Setpoint offset | Degrees past the target to push the downstream setpoint | 4 |
| Minimum cycle time | Seconds to wait between transitions | 300 |
| Control loop interval | Safety-net tick in addition to sensor updates (s) | 30 |
| Start measurement delay | Seconds to ignore stop threshold after cycle start (avoids sensor blowby false stops) | 120 |
| Room sensor stale minutes | If the room sensor hasn't updated in this many minutes, treat it as lost and trigger emergency mode (0 to disable) | 60 |
| Fan tiers (4 tiers) | Maps error-from-target to a fan mode name. Defaults: ≤1°F → `low`, ≤3°F → `medium`, ≤5°F → `high`, everything else → `turbo`. Fan mode names are free-form strings, so any downstream unit's naming works. | See left |
| Emergency fallback | When the room sensor goes offline or stale: optionally force heat/cool based on outdoor temp to protect the room. | Enabled |
| Outdoor temp sensor | For emergency fallback decisions | — |
| Emergency thresholds | Force heat below 40°F outdoor, force cool above 90°F outdoor | 40 / 90 |
| Emergency setpoints | Conservative fixed setpoints during emergency | Heat 62°F, Cool 80°F |
| Emergency fan mode | Fan mode during emergency | `high` |

Emergency mode respects the virtual entity's current `hvac_mode` — if you've set it to `heat_only`, it won't emergency-cool you.

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
