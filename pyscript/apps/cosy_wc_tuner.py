"""
Cosy WC Tuner (pyscript app)

Auto-tunes the Octopus Cosy weather-compensation curve from Home Assistant's
own history.

How it works
------------
The Cosy curve is two numbers: a minimum flow temperature (used in mild
weather) and a maximum flow temperature (used in cold weather), with a
straight line between. The most efficient curve is the LOWEST one that still
holds the room at its target with the compressor running long, steady cycles.

Once a day (and on demand) the app:
  1. Reads the last N hours of recorder history: room temperature, room
     target, zone mode, outdoor temperature and heat pump power input.
  2. Keeps only "comfort" periods (zone in heat/auto, target >= comfort_min),
     skipping the warm-up after a setpoint rise and anything before the last
     curve change has had time to settle. The last change is found from the
     history of the curve sensors, so changes made in the Octopus app count.
  3. Splits each sample's room-temperature error between the MIN and MAX
     ends of the curve according to where the outdoor temperature sat.
  4. Per end: room too cold -> raise it; room too warm -> lower it; room on
     target but the compressor only running a small fraction of the time
     (cycling) -> trim it down.
  5. Moves each end by at most `step` degrees, within hard bounds, keeps a
     minimum gap between min and max, and leaves at least
     `min_days_between_changes` between changes.
  6. Applies through octopus_energy.set_heat_pump_flow_temp_config (unless
     diagnostics_only) and publishes every decision on sensor.cosy_wc_tuner.

Requires the Octopus Energy integration (BottlecapDave) with the Cosy showing
up as heat pump entities, and the recorder keeping their history.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("pyscript.cosy_wc_tuner")
log.setLevel(logging.DEBUG)

VERSION = "1.0.0"

STATUS_SENSOR = "sensor.cosy_wc_tuner"
SET_DOMAIN = "octopus_energy"
SET_SERVICE = "set_heat_pump_flow_temp_config"

# -----------------------------
# Configuration (from configuration.yaml -> pyscript: apps: cosy_wc_tuner:)
# -----------------------------
try:
    # Set when this file lives in <config>/pyscript/apps/
    CFG = pyscript.app_config or {}
except Exception:
    # Fallback if the file was placed directly in <config>/pyscript/
    CFG = (pyscript.config.get("apps") or {}).get("cosy_wc_tuner") or {}
    log.warning("Not loaded as a pyscript app; move this file to <config>/pyscript/apps/")


def _cfg_bool(key, default):
    return str(CFG.get(key, default)).lower() in ("true", "1", "yes", "on")


def _cfg_pair(key, default):
    value = CFG.get(key)
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return default
    return (min(lo, hi), max(lo, hi))


HEAT_PUMP_ID = str(CFG.get("heat_pump_id", ""))
ZONE = str(CFG.get("zone", "zone_1"))
_P = f"octopus_energy_heat_pump_{HEAT_PUMP_ID}"

ENT = {
    "climate": str(CFG.get("climate_entity", f"climate.{_P}_{ZONE}")),
    "outdoor": str(CFG.get("outdoor_temp_sensor", f"sensor.{_P}_live_outdoor_temperature")),
    "power": str(CFG.get("power_sensor", f"sensor.{_P}_live_power_input")),
    "wc_enabled": str(CFG.get("wc_enabled_entity", f"binary_sensor.{_P}_weather_compensation_enabled")),
    "wc_min": str(CFG.get("wc_min_entity", f"sensor.{_P}_weather_compensation_minimum_temperature")),
    "wc_max": str(CFG.get("wc_max_entity", f"sensor.{_P}_weather_compensation_maximum_temperature")),
    "fixed": str(CFG.get("fixed_flow_entity", f"sensor.{_P}_fixed_target_flow_temperature")),
    # Optional: a better room sensor than the zone's own current_temperature.
    "room": str(CFG.get("room_temp_sensor", "")),
}

RUN_AT = str(CFG.get("run_at", "05:30"))
NOTIFY_SERVICE = str(CFG.get("notify_service", ""))
DIAGNOSTICS_ONLY = _cfg_bool("diagnostics_only", False)

LOOKBACK_HOURS = float(CFG.get("lookback_hours", 24.0))      # analysis window
SAMPLE_MINUTES = int(CFG.get("sample_minutes", 5))           # resample grid
SETTLE_HOURS = float(CFG.get("settle_hours", 12.0))          # ignore data this soon after a curve change
MIN_DAYS_BETWEEN_CHANGES = float(CFG.get("min_days_between_changes", 2.0))

# Outdoor temps at which the curve hits max / min flow. Only used to decide
# which END of the curve an error belongs to, so rough values are fine.
CURVE_COLD_OUTDOOR = float(CFG.get("curve_cold_outdoor", -3.0))
CURVE_MILD_OUTDOOR = float(CFG.get("curve_mild_outdoor", 15.0))

COMFORT_MIN_TARGET = float(CFG.get("comfort_min_target", 19.0))  # targets below this are setback
RECOVERY_HOURS = float(CFG.get("recovery_hours", 2.0))           # skip warm-up after a target rise

UNDERSHOOT = float(CFG.get("undershoot", -0.3))   # mean room error below this -> raise
OVERSHOOT = float(CFG.get("overshoot", 0.3))      # mean room error above this -> lower
TRIM_ERROR = float(CFG.get("trim_error", -0.1))   # room at least this close to target...
LOW_DUTY = float(CFG.get("low_duty", 0.6))        # ...and compressor running < this -> trim
RUNNING_KW = float(CFG.get("running_kw", 0.15))   # power input above this = compressor on
MIN_END_HOURS = float(CFG.get("min_end_hours", 3.0))    # weighted data needed to touch an end
MIN_END_SHARE = float(CFG.get("min_end_share", 0.35))   # ...and this share of the window's data

STEP = float(CFG.get("step", 1.0))                # max change per end per run (degC)
MIN_FLOW_BOUNDS = _cfg_pair("min_flow_bounds", (30.0, 45.0))
MAX_FLOW_BOUNDS = _cfg_pair("max_flow_bounds", (40.0, 57.0))
MIN_GAP = float(CFG.get("min_gap", 6.0))          # keep max - min at least this


def _parse_run_at(text):
    try:
        hour, minute = [int(x) for x in str(text).strip().split(":")]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    log.warning("Invalid run_at '%s', using 05:30", text)
    return 5, 30


RUN_HOUR, RUN_MINUTE = _parse_run_at(RUN_AT)

# -----------------------------
# Runtime state (dicts so functions can mutate without `global`)
# -----------------------------
_flags = {"busy": False}
# When we last applied a change, in case the recorder hasn't caught up yet.
_last = {"applied_at": None, "result": None}


# -----------------------------
# Helpers
# -----------------------------
def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(value):
    """Datetime or ISO string -> tz-aware datetime (naive is taken as UTC), or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fnum(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # drop NaN


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _state_obj(entity_id):
    if not entity_id:
        return None
    return hass.states.get(entity_id)


def _float_state(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return None
    return fnum(s.state)


def _attrs(entity_id):
    s = _state_obj(entity_id)
    if s is None:
        return {}
    return dict(getattr(s, "attributes", None) or {})


def power_scale():
    """Multiplier that turns the power sensor's unit into kW."""
    unit = str(_attrs(ENT["power"]).get("unit_of_measurement", "kW")).lower()
    return 0.001 if unit == "w" else 1.0


def series(rows, attr=None, numeric=True):
    """
    History rows -> [(time, value)] sorted by time. The value is the state, or
    an attribute when `attr` is given.
    """
    pts = []
    i = 0
    for row in rows or []:
        t = parse_dt(row.get("t"))
        if t is None:
            continue
        if attr is None:
            v = row.get("state")
        else:
            v = (row.get("attributes") or {}).get(attr)
        if numeric:
            v = fnum(v)
        pts.append((t, i, v))
        i += 1
    pts.sort()
    return [(p[0], p[2]) for p in pts]


def step_sample(pts, grid):
    """Zero-order hold onto the grid."""
    out = []
    i = 0
    cur = None
    for g in grid:
        while i < len(pts) and pts[i][0] <= g:
            cur = pts[i][1]
            i += 1
        out.append(cur)
    return out


# -----------------------------
# History
# -----------------------------
async def fetch_history(entity_ids, start, end):
    """
    {entity_id: [{"t", "state", "attributes"}]} from the recorder, including
    attribute-only changes. "t" is last_updated: the climate entity's room
    and target temperatures are attributes, which don't move last_changed.
    """
    from functools import partial
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    job = partial(get_significant_states, hass, start, end, list(entity_ids),
                  significant_changes_only=False, minimal_response=False, no_attributes=False)
    raw = await get_instance(hass).async_add_executor_job(job)
    out = {}
    for entity_id, states in (raw or {}).items():
        rows = []
        for s in states:
            rows.append({
                "t": getattr(s, "last_updated", None) or getattr(s, "last_changed", None),
                "state": s.state,
                "attributes": dict(getattr(s, "attributes", None) or {}),
            })
        out[entity_id] = rows
    return out


def last_curve_change(hist):
    """When the WC min or max last changed value in the history, or None."""
    last = None
    for key in ("wc_min", "wc_max"):
        prev = None
        for t, v in series(hist.get(ENT[key])):
            if v is None:
                continue  # unavailable / unknown
            if prev is not None and v != prev and (last is None or t > last):
                last = t
            prev = v
    return last


# -----------------------------
# Analysis
# -----------------------------
def end_stats(samples, use_max_weight, step_h):
    """Weighted hours, share of data, mean room error and duty for one end."""
    total = 0.0
    err = 0.0
    duty = 0.0
    for s in samples:
        w = s["w_max"] if use_max_weight else 1.0 - s["w_max"]
        total += w
        err += w * s["err"]
        if s["running"]:
            duty += w
    if total <= 0:
        return {"hours": 0.0, "share": 0.0, "err": None, "duty": None}
    return {
        "hours": round(total * step_h, 2),
        "share": round(total / len(samples), 3),
        "err": round(err / total, 3),
        "duty": round(duty / total, 3),
    }


def analyse(hist, start, end, kw_scale=1.0):
    """Returns (min-end stats, max-end stats, summary) for [start, end)."""
    step = timedelta(minutes=SAMPLE_MINUTES)
    n = int((end - start) / step)
    grid = [start + k * step for k in range(n)]

    clim = hist.get(ENT["climate"])
    mode = step_sample(series(clim, numeric=False), grid)
    target = step_sample(series(clim, "temperature"), grid)
    if ENT["room"]:
        room = step_sample(series(hist.get(ENT["room"])), grid)
    else:
        room = step_sample(series(clim, "current_temperature"), grid)
    tout = step_sample(series(hist.get(ENT["outdoor"])), grid)
    pw = step_sample(series(hist.get(ENT["power"])), grid)

    span = CURVE_MILD_OUTDOOR - CURVE_COLD_OUTDOOR
    recovery = timedelta(hours=RECOVERY_HOURS)
    samples = []
    last_rise = None
    prev_tg = None

    for i in range(n):
        g = grid[i]
        tg = target[i]
        if tg is not None:
            if prev_tg is not None and tg > prev_tg + 0.25:
                last_rise = g
            prev_tg = tg
        if room[i] is None or tg is None or tout[i] is None or pw[i] is None:
            continue
        if mode[i] not in ("heat", "auto"):
            continue
        if tg < COMFORT_MIN_TARGET:
            continue
        if last_rise is not None and g - last_rise < recovery:
            continue
        samples.append({
            "err": room[i] - tg,
            "w_max": clamp((CURVE_MILD_OUTDOOR - tout[i]) / span, 0.0, 1.0),
            "running": pw[i] * kw_scale > RUNNING_KW,
            "outdoor": tout[i],
        })

    step_h = SAMPLE_MINUTES / 60.0
    outdoor_mean = None
    if samples:
        total = 0.0
        for s in samples:
            total += s["outdoor"]
        outdoor_mean = round(total / len(samples), 2)
    summary = {"samples": len(samples), "outdoor_mean": outdoor_mean}
    return end_stats(samples, False, step_h), end_stats(samples, True, step_h), summary


def decide(s):
    """(change in degC, reason) for one end of the curve."""
    if s["err"] is None or s["hours"] < MIN_END_HOURS:
        return 0.0, f"insufficient data ({s['hours']}h)"
    if s["share"] < MIN_END_SHARE:
        return 0.0, f"weather didn't exercise this end ({s['share']:.0%} of data)"
    if s["err"] < UNDERSHOOT:
        return STEP, f"room {s['err']:+.2f}C vs target -> raise"
    if s["err"] > OVERSHOOT:
        return -STEP, f"room {s['err']:+.2f}C vs target -> lower"
    if s["err"] >= TRIM_ERROR and s["duty"] < LOW_DUTY:
        return -STEP, f"on target, compressor duty {s['duty']:.0%} -> trim"
    return 0.0, f"hold (err {s['err']:+.2f}C, duty {s['duty']:.0%})"


def propose(cur_min, cur_max, d_min, d_max, why_min, why_max):
    """Apply bounds and the minimum gap. Returns (new_min, new_max, why_min, why_max)."""
    new_min = clamp(cur_min + d_min, MIN_FLOW_BOUNDS[0], MIN_FLOW_BOUNDS[1])
    new_max = clamp(cur_max + d_max, MAX_FLOW_BOUNDS[0], MAX_FLOW_BOUNDS[1])
    if new_max - new_min < MIN_GAP:
        # Undo whichever move squeezed the gap
        if d_min > 0:
            new_min, why_min = cur_min, why_min + " (blocked: min gap)"
        if d_max < 0 and new_max - new_min < MIN_GAP:
            new_max, why_max = cur_max, why_max + " (blocked: min gap)"
    return new_min, new_max, why_min, why_max


# -----------------------------
# Tuning
# -----------------------------
async def notify(title, message):
    if not NOTIFY_SERVICE:
        return
    domain, _, name = NOTIFY_SERVICE.partition(".")
    if not name:
        domain, name = "notify", domain
    try:
        await hass.services.async_call(domain, name, {"title": title, "message": message}, blocking=False)
    except Exception as e:
        log.warning("Notify via %s failed: %s", NOTIFY_SERVICE, e)


async def tune(reason, allow_apply=True, force=False):
    """Analyse the history and (unless diagnostics_only / preview) apply the new curve."""
    # Serialise: pyscript runs on the event loop, so check-and-set is atomic.
    if _flags["busy"]:
        log.info("[%s] already running, skipped", reason)
        return _last["result"]
    _flags["busy"] = True
    try:
        result = await _tune(reason, allow_apply, force)
    except Exception as e:
        log.exception("[%s] tuning failed", reason)
        result = {"status": "error", "message": str(e)}
    finally:
        _flags["busy"] = False
    result["reason"] = reason
    _last["result"] = result
    publish_status(result)
    return result


async def _tune(reason, allow_apply, force):
    now = now_utc()

    wc = _state_obj(ENT["wc_enabled"])
    if wc is None or wc.state != "on":
        log.info("[%s] weather compensation is off (or %s missing) - not touching it", reason, ENT["wc_enabled"])
        return {"status": "skipped", "message": "weather compensation is off"}
    cur_min = _float_state(ENT["wc_min"])
    cur_max = _float_state(ENT["wc_max"])
    if cur_min is None or cur_max is None:
        log.warning("[%s] couldn't read the current curve from %s / %s", reason, ENT["wc_min"], ENT["wc_max"])
        return {"status": "skipped", "message": "couldn't read the current curve"}
    fixed = _float_state(ENT["fixed"])
    if fixed is None:
        fixed = cur_max  # the service needs a value; it's unused while WC is on

    result = {"current_min": cur_min, "current_max": cur_max}

    ids = [ENT["climate"], ENT["outdoor"], ENT["power"], ENT["wc_min"], ENT["wc_max"]]
    if ENT["room"]:
        ids.append(ENT["room"])
    fetch_hours = max(LOOKBACK_HOURS, SETTLE_HOURS, MIN_DAYS_BETWEEN_CHANGES * 24)
    hist = await fetch_history(ids, now - timedelta(hours=fetch_hours), now)

    last_change = last_curve_change(hist)
    applied_at = _last["applied_at"]
    if applied_at is not None and (last_change is None or applied_at > last_change):
        last_change = applied_at
    result["last_change"] = last_change.isoformat() if last_change else None

    # Window: last lookback, but not before the last change has settled
    start = now - timedelta(hours=LOOKBACK_HOURS)
    if last_change is not None:
        start = max(start, last_change + timedelta(hours=SETTLE_HOURS))
    result["window_start"] = start.isoformat()
    if start >= now - timedelta(hours=MIN_END_HOURS):
        log.info("[%s] not enough settled data since the last change at %s", reason, last_change)
        result.update({"status": "waiting_settle", "proposed_min": cur_min, "proposed_max": cur_max})
        return result

    s_min, s_max, summary = analyse(hist, start, now, power_scale())
    d_min, why_min = decide(s_min)
    d_max, why_max = decide(s_max)
    new_min, new_max, why_min, why_max = propose(cur_min, cur_max, d_min, d_max, why_min, why_max)

    result.update({
        "samples": summary["samples"],
        "outdoor_mean": summary["outdoor_mean"],
        "min_end": s_min, "min_decision": why_min,
        "max_end": s_max, "max_decision": why_max,
        "proposed_min": new_min, "proposed_max": new_max,
    })
    log.info("[%s] window %s -> %s, %s comfort samples, mean outdoor %sC",
             reason, start.isoformat(), now.isoformat(), summary["samples"], summary["outdoor_mean"])
    log.info("[%s]   MIN end %s -> %s", reason, s_min, why_min)
    log.info("[%s]   MAX end %s -> %s", reason, s_max, why_max)
    log.info("[%s]   curve %g/%gC -> %g/%gC", reason, cur_min, cur_max, new_min, new_max)

    if (new_min, new_max) == (cur_min, cur_max):
        result["status"] = "no_change"
        return result

    if last_change is not None and not force:
        days = (now - last_change).total_seconds() / 86400
        if days < MIN_DAYS_BETWEEN_CHANGES:
            log.info("[%s] change wanted but the last change was %.1f days ago - waiting", reason, days)
            result["status"] = "waiting_min_days"
            return result

    curve = f"{cur_min:g}/{cur_max:g}C -> {new_min:g}/{new_max:g}C"
    if DIAGNOSTICS_ONLY or not allow_apply:
        result["status"] = "proposed"
        if allow_apply:
            await notify("Cosy curve change proposed", f"{curve} (diagnostics_only, not applied). "
                         f"Min: {why_min}. Max: {why_max}.")
        return result

    await hass.services.async_call(SET_DOMAIN, SET_SERVICE, {
        "entity_id": ENT["climate"],
        "weather_comp_enabled": True,
        "weather_comp_min_temperature": new_min,
        "weather_comp_max_temperature": new_max,
        "fixed_flow_temperature": fixed,
    }, blocking=True)
    _last["applied_at"] = now
    result["status"] = "applied"
    result["last_change"] = now.isoformat()
    log.info("[%s] applied %s", reason, curve)
    await notify("Cosy curve changed", f"{curve}. Min: {why_min}. Max: {why_max}.")
    return result


def publish_status(result):
    attrs = {
        "current_min": None, "current_max": None,
        "proposed_min": None, "proposed_max": None,
        "min_end": None, "min_decision": None,
        "max_end": None, "max_decision": None,
        "samples": None, "outdoor_mean": None,
        "window_start": None, "last_change": None,
        "message": None, "reason": None,
    }
    for key, value in result.items():
        if key != "status":
            attrs[key] = value
    attrs.update({
        "diagnostics_only": DIAGNOSTICS_ONLY,
        "run_at": f"{RUN_HOUR:02d}:{RUN_MINUTE:02d}",
        "version": VERSION,
        "last_updated": now_utc().isoformat(),
        "friendly_name": "Cosy WC Tuner",
        "icon": "mdi:heat-pump-outline",
    })
    state.set(STATUS_SENSOR, result.get("status", "idle"), new_attributes=attrs)


# -----------------------------
# Triggers
# -----------------------------
@time_trigger("startup")
async def cosy_wc_tuner_startup():
    log.info("=== Cosy WC Tuner v%s starting (runs daily at %02d:%02d, diagnostics_only=%s) ===",
             VERSION, RUN_HOUR, RUN_MINUTE, DIAGNOSTICS_ONLY)
    missing = []
    for key in ("climate", "outdoor", "power", "wc_enabled", "wc_min", "wc_max", "room"):
        if ENT[key] and hass.states.get(ENT[key]) is None:
            missing.append(ENT[key])
    if missing:
        log.warning("Entities not found (yet): %s - check heat_pump_id / zone or the *_entity options",
                    ", ".join(missing))
    publish_status({
        "status": "idle",
        "current_min": _float_state(ENT["wc_min"]),
        "current_max": _float_state(ENT["wc_max"]),
        "reason": "startup",
    })


@time_trigger(f"cron({RUN_MINUTE} {RUN_HOUR} * * *)")
async def cosy_wc_tuner_daily():
    await tune("daily")


# -----------------------------
# Services (Developer Tools -> Actions)
# -----------------------------
@service
async def cosy_wc_tuner_run(force=False):
    """Analyse and apply the curve change now (diagnostics_only still applies). force=true skips the min-days guard."""
    await tune("manual", force=str(force).lower() in ("true", "1", "yes", "on"))


@service
async def cosy_wc_tuner_preview():
    """Analyse and log what would change, without touching the heat pump."""
    await tune("preview", allow_apply=False)
