#!/usr/bin/env python3
"""
cosy_wc_tuner.py - auto-tune the Octopus Cosy weather-compensation curve.

How it works
------------
The Cosy curve is just two numbers: a minimum flow temp (used in mild weather)
and a maximum flow temp (used in cold weather), with a straight line between.
The most efficient curve is the LOWEST one that still holds the room at its
target with the compressor running long, steady cycles.

Once a day this script:
  1. Pulls the last N hours of history from Home Assistant: room temp, room
     target, zone mode, outdoor temp, heat pump power input.
  2. Keeps only "comfort" periods (zone in heat/auto, target >= COMFORT_MIN),
     skipping the warm-up window after a setpoint rise and anything before the
     last curve change has had time to settle.
  3. Splits each sample's room-temperature error between the MIN and MAX ends
     of the curve according to where the outdoor temp sat on the curve.
  4. Per end: room too cold -> raise it; room too warm -> lower it; room on
     target but compressor only running a small fraction of the time (i.e.
     cycling) -> trim it down.
  5. Moves each end by at most STEP degrees, within hard bounds, keeps a
     minimum gap between min and max, and enforces a settle period between
     changes.
  6. Applies via octopus_energy.set_heat_pump_flow_temp_config (only with
     --apply; default is a dry run) and logs every decision to a JSONL file.

Requirements
------------
- Home Assistant with the Octopus Energy integration (BottlecapDave) and the
  Cosy showing up as heat pump entities.
- A long-lived access token (HA profile -> Security).
- Python 3.9+, `pip install requests`.

Usage
-----
  export HA_URL=http://homeassistant.local:8123
  export HA_TOKEN=eyJ...
  export COSY_HP_ID=<the id in your entity names>
  export COSY_ZONE=<zone code in the climate entity name>
  python3 cosy_wc_tuner.py            # dry run, prints what it would do
  python3 cosy_wc_tuner.py --apply    # actually change the curve

Run it once a day (e.g. cron at 05:30) after a dry-run week you're happy with.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HP_ID = os.environ.get("COSY_HP_ID", "CHANGE_ME")
ZONE = os.environ.get("COSY_ZONE", "zone_1")  # check your climate entity name

P = f"octopus_energy_heat_pump_{HP_ID}"
ENT = {
    "climate": f"climate.{P}_{ZONE}",
    "outdoor": f"sensor.{P}_live_outdoor_temperature",
    "power": f"sensor.{P}_live_power_input",
    "wc_enabled": f"binary_sensor.{P}_weather_compensation_enabled",
    "wc_min": f"sensor.{P}_weather_compensation_minimum_temperature",
    "wc_max": f"sensor.{P}_weather_compensation_maximum_temperature",
    "fixed": f"sensor.{P}_fixed_target_flow_temperature",
    # Optional: a better room sensor than the zone's own current_temperature.
    # Leave as None to use the climate entity's current_temperature attribute.
    "room_override": os.environ.get("COSY_ROOM_SENSOR") or None,
}


@dataclass
class Cfg:
    lookback_h: float = 24.0        # analysis window
    sample_min: int = 5             # resample grid
    settle_h: float = 12.0          # ignore data this soon after a curve change
    min_days_between_changes: float = 2.0

    # Outdoor temps at which the curve hits max / min flow. Only used to decide
    # which END of the curve an error belongs to, so rough values are fine.
    # Check against what the Cosy app shows for your curve.
    curve_cold_out: float = -3.0
    curve_mild_out: float = 15.0

    comfort_min: float = 19.0       # target temps below this count as setback
    recovery_h: float = 2.0         # skip warm-up after a target increase

    undershoot: float = -0.3        # mean room error below this -> raise
    overshoot: float = 0.3          # mean room error above this -> lower
    trim_err: float = -0.1          # room at least this close to target...
    low_duty: float = 0.6           # ...and compressor running < this -> trim
    running_kw: float = 0.15        # power input above this = compressor on
    min_end_hours: float = 3.0      # need this much weighted data to touch an end
    min_end_share: float = 0.35     # ...and this share of the window's data, so a
                                    # mild day can't move the cold-weather end

    step: float = 1.0               # max change per end per run (degC)
    min_bounds: tuple = (30.0, 45.0)
    max_bounds: tuple = (40.0, 57.0)  # your radiators/install sizing sets the top
    min_gap: float = 6.0            # keep max - min at least this

    state_file: str = os.path.expanduser("~/.cosy_wc_tuner_state.json")
    log_file: str = os.path.expanduser("~/.cosy_wc_tuner_log.jsonl")


CFG = Cfg()

# --------------------------------------------------------------------------
# Home Assistant helpers
# --------------------------------------------------------------------------

SESSION = requests.Session()


def ha_headers():
    if not HA_TOKEN:
        sys.exit("HA_TOKEN not set")
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def get_state(entity_id: str) -> dict:
    r = SESSION.get(f"{HA_URL}/api/states/{entity_id}", headers=ha_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def get_history(entity_ids: list[str], start: dt.datetime, end: dt.datetime) -> dict:
    url = f"{HA_URL}/api/history/period/{quote(start.isoformat())}"
    params = {
        "filter_entity_id": ",".join(entity_ids),
        "end_time": end.isoformat(),
        "significant_changes_only": "0",  # we need attribute changes on the climate entity
    }
    r = SESSION.get(url, headers=ha_headers(), params=params, timeout=120)
    r.raise_for_status()
    out = {}
    for series in r.json():
        if series:
            out[series[0]["entity_id"]] = series
    return out


def call_service(domain: str, service: str, data: dict):
    r = SESSION.post(f"{HA_URL}/api/services/{domain}/{service}",
                     headers=ha_headers(), json=data, timeout=60)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------
# Time-series helpers
# --------------------------------------------------------------------------

def fnum(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def points(series: list, extract) -> list:
    pts = []
    for e in series or []:
        ts = parse_ts(e.get("last_changed") or e.get("last_updated"))
        pts.append((ts, extract(e)))
    pts.sort(key=lambda p: p[0])
    return pts


def step_sample(pts: list, grid: list) -> list:
    """Zero-order hold onto the grid."""
    out, i, cur = [], 0, None
    for g in grid:
        while i < len(pts) and pts[i][0] <= g:
            cur = pts[i][1]
            i += 1
        out.append(cur)
    return out


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

@dataclass
class EndStats:
    hours: float
    share: float
    err: float | None
    duty: float | None


def analyse(hist: dict, start: dt.datetime, end: dt.datetime, cfg: Cfg):
    step = dt.timedelta(minutes=cfg.sample_min)
    n = int((end - start) / step)
    grid = [start + k * step for k in range(n)]

    clim = hist.get(ENT["climate"], [])
    mode = step_sample(points(clim, lambda e: e.get("state")), grid)
    target = step_sample(points(clim, lambda e: fnum(e.get("attributes", {}).get("temperature"))), grid)
    if ENT["room_override"]:
        room = step_sample(points(hist.get(ENT["room_override"], []), lambda e: fnum(e.get("state"))), grid)
    else:
        room = step_sample(points(clim, lambda e: fnum(e.get("attributes", {}).get("current_temperature"))), grid)
    tout = step_sample(points(hist.get(ENT["outdoor"], []), lambda e: fnum(e.get("state"))), grid)
    pw = step_sample(points(hist.get(ENT["power"], []), lambda e: fnum(e.get("state"))), grid)

    span = cfg.curve_mild_out - cfg.curve_cold_out
    recovery = dt.timedelta(hours=cfg.recovery_h)
    rows, last_rise, prev_tg = [], None, None

    for i, g in enumerate(grid):
        tg = target[i]
        if tg is not None:
            if prev_tg is not None and tg > prev_tg + 0.25:
                last_rise = g
            prev_tg = tg
        if None in (room[i], tg, tout[i], pw[i]):
            continue
        if mode[i] not in ("heat", "auto"):
            continue
        if tg < cfg.comfort_min:
            continue
        if last_rise is not None and g - last_rise < recovery:
            continue
        w_max = clamp((cfg.curve_mild_out - tout[i]) / span, 0.0, 1.0)
        rows.append((room[i] - tg, w_max, pw[i] > cfg.running_kw, tout[i]))

    step_h = cfg.sample_min / 60.0

    def stats(weight_fn) -> EndStats:
        W = sum(weight_fn(r) for r in rows)
        if W <= 0:
            return EndStats(0.0, 0.0, None, None)
        err = sum(weight_fn(r) * r[0] for r in rows) / W
        duty = sum(weight_fn(r) * (1.0 if r[2] else 0.0) for r in rows) / W
        return EndStats(round(W * step_h, 2), round(W / len(rows), 3), round(err, 3), round(duty, 3))

    summary = {
        "samples": len(rows),
        "outdoor_mean": round(sum(r[3] for r in rows) / len(rows), 2) if rows else None,
    }
    return stats(lambda r: 1.0 - r[1]), stats(lambda r: r[1]), summary


def decide(s: EndStats, cfg: Cfg) -> tuple[float, str]:
    if s.hours < cfg.min_end_hours or s.err is None:
        return 0.0, f"insufficient data ({s.hours}h)"
    if s.share < cfg.min_end_share:
        return 0.0, f"weather didn't exercise this end ({s.share:.0%} of data)"
    if s.err < cfg.undershoot:
        return +cfg.step, f"room {s.err:+.2f}C vs target -> raise"
    if s.err > cfg.overshoot:
        return -cfg.step, f"room {s.err:+.2f}C vs target -> lower"
    if s.err >= cfg.trim_err and s.duty < cfg.low_duty:
        return -cfg.step, f"on target, compressor duty {s.duty:.0%} -> trim"
    return 0.0, f"hold (err {s.err:+.2f}C, duty {s.duty:.0%})"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(cfg: Cfg) -> dict:
    try:
        return json.loads(Path(cfg.state_file).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(cfg: Cfg, state: dict):
    Path(cfg.state_file).write_text(json.dumps(state, indent=2))


def log(cfg: Cfg, record: dict):
    with open(cfg.log_file, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually change the curve (default: dry run)")
    ap.add_argument("--force", action="store_true", help="ignore the min-days-between-changes guard")
    args = ap.parse_args()
    cfg = CFG

    now = dt.datetime.now(dt.timezone.utc)
    state = load_state(cfg)

    # Current curve
    if get_state(ENT["wc_enabled"])["state"] != "on":
        sys.exit("Weather compensation is off on the Cosy - not touching it.")
    cur_min = fnum(get_state(ENT["wc_min"])["state"])
    cur_max = fnum(get_state(ENT["wc_max"])["state"])
    fixed = fnum(get_state(ENT["fixed"])["state"])
    if cur_min is None or cur_max is None:
        sys.exit("Couldn't read current curve from HA.")
    if fixed is None:
        fixed = cur_max  # service requires a value; it's unused while WC is on

    # Window: last lookback_h, but not before the last change has settled
    start = now - dt.timedelta(hours=cfg.lookback_h)
    last_change = state.get("last_change")
    if last_change:
        lc = parse_ts(last_change)
        start = max(start, lc + dt.timedelta(hours=cfg.settle_h))
    if start >= now - dt.timedelta(hours=cfg.min_end_hours):
        print("Not enough settled data since last change - skipping.")
        return

    ids = [ENT["climate"], ENT["outdoor"], ENT["power"]]
    if ENT["room_override"]:
        ids.append(ENT["room_override"])
    hist = get_history(ids, start, now)

    s_min, s_max, summary = analyse(hist, start, now, cfg)
    d_min, why_min = decide(s_min, cfg)
    d_max, why_max = decide(s_max, cfg)

    new_min = clamp(cur_min + d_min, *cfg.min_bounds)
    new_max = clamp(cur_max + d_max, *cfg.max_bounds)
    if new_max - new_min < cfg.min_gap:
        # Undo whichever move squeezed the gap
        if d_min > 0:
            new_min, why_min = cur_min, why_min + " (blocked: min gap)"
        if d_max < 0 and new_max - new_min < cfg.min_gap:
            new_max, why_max = cur_max, why_max + " (blocked: min gap)"

    changed = (new_min, new_max) != (cur_min, cur_max)

    too_soon = False
    if changed and last_change and not args.force:
        days = (now - parse_ts(last_change)).total_seconds() / 86400
        too_soon = days < cfg.min_days_between_changes

    record = {
        "time": now.isoformat(),
        "window_start": start.isoformat(),
        "summary": summary,
        "min_end": asdict(s_min), "min_decision": why_min,
        "max_end": asdict(s_max), "max_decision": why_max,
        "current": [cur_min, cur_max],
        "proposed": [new_min, new_max],
        "applied": False,
    }

    print(f"Window {start:%d %b %H:%M} -> {now:%d %b %H:%M} UTC, "
          f"{summary['samples']} comfort samples, mean outdoor {summary['outdoor_mean']}C")
    print(f"  MIN end: {asdict(s_min)}  -> {why_min}")
    print(f"  MAX end: {asdict(s_max)}  -> {why_max}")
    print(f"  Curve: {cur_min:g}/{cur_max:g}C  ->  {new_min:g}/{new_max:g}C")

    if not changed:
        print("No change.")
    elif too_soon:
        print(f"Change wanted but last change was under {cfg.min_days_between_changes} days ago - waiting.")
    elif not args.apply:
        print("Dry run - pass --apply to set it.")
    else:
        call_service("octopus_energy", "set_heat_pump_flow_temp_config", {
            "entity_id": ENT["climate"],
            "weather_comp_enabled": True,
            "weather_comp_min_temperature": new_min,
            "weather_comp_max_temperature": new_max,
            "fixed_flow_temperature": fixed,
        })
        state["last_change"] = now.isoformat()
        state["history"] = (state.get("history", []) + [[now.isoformat(), new_min, new_max]])[-60:]
        save_state(cfg, state)
        record["applied"] = True
        print("Applied.")

    log(cfg, record)


if __name__ == "__main__":
    main()
