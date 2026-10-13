# Cosy WC Tuner

A [pyscript](https://github.com/custom-components/pyscript) app for Home Assistant that tunes the **weather-compensation curve** of an **Octopus Cosy** heat pump. Each day it looks at how the house actually behaved and nudges the curve.

It's a companion to [Solis EV-Follow](https://github.com/darvds/SolisOctopusEVFollow) and [Solis Power Down](https://github.com/darvds/SolisOctopusPowerDown), and runs in the same `pyscript:` block.

## Why

The Cosy's weather-compensation curve is two numbers: a **minimum** flow temperature for mild weather and a **maximum** for cold weather, with a straight line between them. The most efficient curve is the **lowest** one that still holds the room at its target, with the compressor running long, steady cycles. Finding it by hand means changing a number, waiting a few days, and checking again. This app does that loop for you.

## How it works

Once a day at `run_at` (or when you run it from Developer Tools):

1. **Read history.** From the recorder it reads the last `lookback_hours` of:
   - room temperature and target, and the zone's mode
   - outdoor temperature
   - heat pump power input
2. **Filter.** It keeps only comfort periods:
   - the zone is in `heat` or `auto`
   - the target is at least `comfort_min_target`
   - not within `recovery_hours` after the target went up
   - not within `settle_hours` after the curve last changed

   The last change is found from the history of the curve sensors. Changes you make in the Octopus app count as well as the app's own.
3. **Split by outdoor temperature.** Each sample's room error is shared between the two ends of the curve according to where the outdoor temperature sat between `curve_cold_outdoor` and `curve_mild_outdoor`. A 15 °C day only counts towards the min end. A −3 °C day only counts towards the max end.
4. **Decide, per end.**
   - **Too cold** (mean error below `undershoot`): raise it.
   - **Too warm** (above `overshoot`): lower it.
   - **On target but cycling** (error at or above `trim_error`, compressor running less than `low_duty` of the time): trim it down.
   - **Otherwise:** hold.

   An end is only touched if it has at least `min_end_hours` of weighted data and `min_end_share` of the window. This stops a mild day from moving the cold-weather end.
5. **Limit.** Each end moves by at most `step` °C per run, stays inside `min_flow_bounds` and `max_flow_bounds`, and the two ends stay at least `min_gap` apart. Changes are at least `min_days_between_changes` apart.
6. **Apply.** It calls `octopus_energy.set_heat_pump_flow_temp_config` and publishes the decision on `sensor.cosy_wc_tuner`.

**Fail-safe:** the app only ever writes a new curve within your bounds. If Home Assistant or pyscript stops, the Cosy keeps running on the last curve. It never touches anything while weather compensation is switched off.

### Example

The example uses a 35/50 °C curve with a 20 °C target. The last 24 hours were at 6 °C outside (half way along the curve), and the room averaged 19.5 °C:

| | Min end | Max end |
|---|---|---|
| Weighted data | 12 h, 50% | 12 h, 50% |
| Mean room error | −0.5 °C | −0.5 °C |
| Decision | raise | raise |
| New curve | **36** | **51** |

## Requirements

- Home Assistant with [pyscript](https://github.com/custom-components/pyscript) (HACS).
- [Octopus Energy integration](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) with your Cosy showing as heat pump entities:
  - `climate.octopus_energy_heat_pump_<id>_<zone>`
  - `sensor.octopus_energy_heat_pump_<id>_live_outdoor_temperature`
  - `sensor.octopus_energy_heat_pump_<id>_live_power_input`
  - `binary_sensor.octopus_energy_heat_pump_<id>_weather_compensation_enabled`
  - `sensor.octopus_energy_heat_pump_<id>_weather_compensation_minimum_temperature` / `..._maximum_temperature`
  - `sensor.octopus_energy_heat_pump_<id>_fixed_target_flow_temperature`
- Weather compensation switched **on** in the Cosy.
- The recorder keeping at least 2 days of history for those entities. The default `purge_keep_days` of 10 is fine.

## Installation

1. Copy `pyscript/apps/cosy_wc_tuner.py` to `<config>/pyscript/apps/cosy_wc_tuner.py`.
2. Merge the block from [`configuration.example.yaml`](configuration.example.yaml) into `configuration.yaml`. Keep a single `pyscript:` block.
3. Set `heat_pump_id` and `zone` from your entity names. If an entity is named differently, override it, e.g. with `climate_entity`.
4. Set `curve_cold_outdoor` / `curve_mild_outdoor` to match your curve in the Octopus app, and the bounds to what your install can take.
5. Restart Home Assistant.

Start with `diagnostics_only: true`. Run **`pyscript.cosy_wc_tuner_preview`** and check `sensor.cosy_wc_tuner`. The startup log warns about any entity it can't find. Leave it in diagnostics for a week or so and watch the `proposed` decisions. When they look sensible, set `diagnostics_only: false` and restart.

## Configuration

| Option | Default | Description |
|---|---|---|
| `heat_pump_id` | **required** (unless every entity is set) | The id in your heat pump entity names |
| `zone` | `zone_1` | Zone code in the climate entity name |
| `climate_entity`, `outdoor_temp_sensor`, `power_sensor`, `wc_enabled_entity`, `wc_min_entity`, `wc_max_entity`, `fixed_flow_entity` | from `heat_pump_id` | Override individual entities |
| `room_temp_sensor` | none | A better room sensor than the zone's `current_temperature` |
| `run_at` | `05:30` | Daily run time (HA local time) |
| `curve_cold_outdoor` / `curve_mild_outdoor` | `-3` / `15` | Outdoor temps where the curve reaches max / min flow |
| `min_flow_bounds` | `[30, 45]` | Range the min end may move within |
| `max_flow_bounds` | `[40, 57]` | Range the max end may move within |
| `min_gap` | `6` | Minimum max − min |
| `step` | `1.0` | Largest change per end per run (°C) |
| `min_days_between_changes` | `2` | Days between changes |
| `settle_hours` | `12` | Data this soon after a curve change is ignored |
| `lookback_hours` | `24` | Analysis window |
| `comfort_min_target` | `19` | Targets below this count as setback and are ignored |
| `recovery_hours` | `2` | Warm-up after a target rise that is ignored |
| `undershoot` / `overshoot` | `-0.3` / `0.3` | Mean room error (°C) that triggers a raise / lower |
| `trim_error` / `low_duty` | `-0.1` / `0.6` | Trim when the room is at least this close and the compressor runs less than this share |
| `running_kw` | `0.15` | Power input above this counts as the compressor running (W sensors are converted) |
| `min_end_hours` / `min_end_share` | `3` / `0.35` | Data an end needs before it's touched |
| `sample_minutes` | `5` | Resampling grid |
| `notify_service` | none | e.g. `notify.mobile_app_phone`: a message when the curve changes (or would, in diagnostics) |
| `diagnostics_only` | `false` | Work out and publish changes without applying them |

## Monitoring

- **`sensor.cosy_wc_tuner`**
  - **State:** one of the following.
    - `idle`: started, not run yet.
    - `no_change`: both ends hold.
    - `proposed`: a change is wanted but not applied (diagnostics or preview).
    - `applied`: the curve was changed.
    - `waiting_settle`: not enough data since the last change.
    - `waiting_min_days`: a change is wanted but the last one was too recent.
    - `skipped`: weather compensation is off, or the curve couldn't be read.
    - `error`: see `message` and the log.
  - **Attributes:**
    - `current_min/max` and `proposed_min/max`
    - `min_end` / `max_end`: weighted `hours`, `share`, mean `err` and `duty`
    - `min_decision` / `max_decision`
    - `samples`, `outdoor_mean`, `window_start`, `last_change`, `reason`

  The recorder keeps the attribute history, so you can see every decision over time.
- **Actions** (Developer Tools → Actions):
  - `pyscript.cosy_wc_tuner_run`: analyse and apply now (respects `diagnostics_only`). Use `force: true` to skip the min-days guard.
  - `pyscript.cosy_wc_tuner_preview`: analyse and publish without touching the heat pump.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      pyscript.cosy_wc_tuner: debug
  ```

## Notes

- `curve_cold_outdoor` / `curve_mild_outdoor` only decide which end an error belongs to, so rough values are fine.
- `fixed_flow_temperature` is sent unchanged (the service requires it). It isn't used while weather compensation is on.
- The zone's own temperature reading is often a single sensor near the controller. A room sensor in the main living space (`room_temp_sensor`) usually gives better results.

## Development

The tests use only the Python standard library (3.9+):

```bash
python3 -m unittest discover -s tests -t .
```

`tests/harness.py` loads the app with fakes for:
- pyscript's globals
- the clock
- HA states and services
- recorder history

The tests cover:
- **Analysis:** how the error is split between the ends, setback / off / recovery filtering, duty from kW and W sensors, the room sensor override, and `last_updated` for attribute-only changes
- **Decisions:** raise / lower / trim / hold, data thresholds, bounds and the min-gap rule
- **Tuning:** diagnostics vs apply, the service payload, settle and min-days guards (including changes made in the Octopus app), force, notifications, errors and the status sensor
- **Triggers and services**

## Credits

- [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy): Cosy heat pump entities and the flow temperature action.

Not affiliated with Octopus Energy or Home Assistant.

## License

[MIT](LICENSE)
