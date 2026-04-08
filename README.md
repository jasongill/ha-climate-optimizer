# Climate Optimizer

A Home Assistant custom integration that wraps a "dumb" climate device (e.g. a minisplit exposed via the ESPHome `aux_ac` or `midea` components) with a virtual climate entity driven by an external temperature/humidity sensor in the same room.

## Why

Minisplits in `auto` / `heat_cool` mode often:

- Let the room drift several degrees before reacting, because the unit's internal sensor is near the ceiling / in a corner and lags the actual room temperature.
- Sit there running the indoor fan on low 24/7 even when no temperature change is happening.

This integration gives you a *virtual* climate entity per zone. You pick:

- a **room temperature sensor** (e.g. an Xsense / thermo-hygrometer entity)
- the **downstream climate entity** (the minisplit)
- a **target range** (heat target + cool target with a small hysteresis deadband)

The virtual entity runs a simple bang-bang state machine with the following behavior:

- When the room is *above* `cool_target + deadband`: put the downstream in `cool`, push its setpoint `offset` degrees **below** your cool target (e.g. cool target 70 → minisplit setpoint 66) so it actually runs instead of thinking it's already at temp, and pick a fan mode based on how far out of range we are.
- When the room is *below* `heat_target - deadband`: mirror image for heat.
- When the room re-enters the target band: turn the downstream **off** (so you don't have the indoor fan running 24/7), and wait `min_cycle_time` before starting another cycle.

## Install (local / development)

1. Copy `custom_components/climate_optimizer` into your HA config directory:
   ```
   config/custom_components/climate_optimizer/
   ```
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Climate Optimizer**.
4. Fill in the zone name, room sensor, downstream climate, and target range.

Each zone is a separate config entry, so add one per minisplit.

## Configuration options

| Field | Meaning | Default |
| --- | --- | --- |
| Zone name | Name for the virtual climate entity | — |
| Room temperature sensor | Temperature sensor to read | — |
| Room humidity sensor | Optional, used for display/logging | — |
| Downstream climate entity | The real minisplit to command | — |
| Heat target | Below this, start heating (°F) | 65 |
| Cool target | Above this, start cooling (°F) | 70 |
| Deadband | Hysteresis before starting a cycle (°F) | 0.5 |
| Setpoint offset | Degrees past the target to push the minisplit's own setpoint | 4 |
| Minimum cycle time | Seconds to wait between transitions | 300 |
| Control loop interval | Safety-net tick in addition to sensor updates | 30 |

You can tune all numeric fields later without re-adding the entry via **Configure** on the integration entry.

## Fan tiering

The integration picks the downstream fan mode based on how far out of the target band you are. The default tiers (configured in `const.py` for now) are:

| Error from target band | Fan mode |
| --- | --- |
| ≤ 1°F | `low` |
| ≤ 3°F | `medium` |
| ≤ 5°F | `high` |
| > 5°F | `turbo` |

If a tier's fan_mode isn't available on the downstream unit, the integration falls back to the nearest supported mode. Arbitrary fan_mode strings are supported, so this should work with future units that expose different fan names.

## Safety & emergency fallback

- If the downstream unit becomes unavailable, the integration skips that tick and retries later.
- Downstream commands are de-duped so nothing is resent if the unit is already in the desired state.
- The minimum cycle time protects the compressor from rapid on/off cycling.

When the **room sensor** becomes unknown/unavailable, you have two behaviors:

1. **Emergency fallback disabled** — the downstream unit is turned off (safer than running blind).
2. **Emergency fallback enabled** (default) — the integration consults an optional **outdoor temperature sensor** and, if the outdoor temperature is outside the configured safe band, forces heat or cool at a conservative fixed setpoint using a conservative fan mode. This is intended to keep pipes from freezing (and, symmetrically, keep the room from cooking) even if the room sensor has fallen off the network.

  Defaults:
  - Force heat when outdoor < **35°F**, at a setpoint of **62°F** on `low` fan
  - Force cool when outdoor > **95°F**, at a setpoint of **80°F** on `low` fan

  If no outdoor sensor is configured (or it is also unavailable), the unit is turned off — the integration never runs blind. Emergency mode still respects the virtual entity's `hvac_mode` (e.g. if you set it to `heat`, it will not emergency-cool).

An `emergency_active` attribute on the virtual entity tells you when this is engaged, and the HA log will show a warning explaining why.

## Control source

The virtual entity is intended to be the **only** way the downstream unit is controlled. The integration will happily overwrite any changes made directly to the downstream climate entity on the next tick; treat the downstream as an implementation detail and drive everything through the virtual entity.

## Status

v0.1 — runs locally, has not been published to HACS yet.
