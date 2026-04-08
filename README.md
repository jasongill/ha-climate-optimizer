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
- **Stop** (turn the downstream unit fully off) when the room re-enters the target band exactly, then wait `min_cycle_time` before another transition is allowed.
- On every tick, the commanded fan mode is re-evaluated based on the current error from the target band and the configured fan tiers.

Downstream commands are de-duplicated — the integration only resends mode/setpoint/fan changes when they actually differ from the downstream entity's current state.

## Configuration options

All fields are set in the UI when you add the integration, and every numeric field is editable later via **Configure** on the integration entry.

| Field | Meaning | Default |
| --- | --- | --- |
| Virtual Climate Device Name | Name for the virtual climate entity | — |
| Room temperature sensor | Temperature sensor to read | — |
| Room humidity sensor | Optional, used for display | — |
| Downstream climate entity | The real mini split to command | — |
| Area | Optional area assignment for the device | — |
| Heat target | Below this, start heating (°F) | 65 |
| Cool target | Above this, start cooling (°F) | 70 |
| Deadband | Hysteresis before starting a cycle (°F) | 0.5 |
| Setpoint offset | Degrees past the target to push the downstream setpoint | 4 |
| Minimum cycle time | Seconds to wait between transitions | 300 |
| Control loop interval | Safety-net tick in addition to sensor updates (s) | 30 |

## Fan tiering

The integration maps "how far out of the target band are we" to a downstream fan mode. Four tiers are configurable directly in the UI, each a `(max_error, fan_mode)` pair. Defaults:

| Error from target band | Fan mode |
| --- | --- |
| ≤ 1°F | `low` |
| ≤ 3°F | `medium` |
| ≤ 5°F | `high` |
| everything else | `turbo` |

Fan mode names are free-form strings, so any downstream unit's naming works. If the exact mode name isn't supported by the downstream entity, the integration falls back to the nearest available mode.

## Emergency fallback

When the room sensor becomes `unknown` or `unavailable`, the integration has two possible behaviors:

1. **Disabled** — the downstream unit is turned off. Safer than running blind.
2. **Enabled (default)** — the integration consults an optional outdoor temperature sensor. If the outdoor temperature is outside a configured safe band, it forces heat or cool at a conservative fixed setpoint and fan mode until the room sensor recovers. If no outdoor sensor is configured (or it is also unavailable), the downstream unit is turned off.

Defaults:

- Force heat when outdoor is below **35°F**, at a setpoint of **62°F** on `low` fan
- Force cool when outdoor is above **95°F**, at a setpoint of **80°F** on `low` fan

Emergency mode still respects the virtual entity's current `hvac_mode` — if you've set it to `heat_only`, it won't emergency-cool you.

An `emergency_active` attribute is exposed on the virtual entity, and a warning is logged when emergency mode engages or disengages.

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
