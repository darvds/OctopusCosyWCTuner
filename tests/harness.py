"""
Test harness: loads the pyscript app outside Home Assistant.

pyscript injects globals (pyscript, hass, state, task, service, time_trigger)
into each script, so the app can't be imported normally. The harness compiles
the file into a fresh namespace with fakes for all of those, plus a fixed
clock and a fake recorder history.
"""
import asyncio
import logging
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "pyscript" / "apps" / "cosy_wc_tuner.py"

NOW = datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)

_P = "octopus_energy_heat_pump_test"
CLIMATE = f"climate.{_P}_zone_1"
OUTDOOR = f"sensor.{_P}_live_outdoor_temperature"
POWER = f"sensor.{_P}_live_power_input"
WC_ENABLED = f"binary_sensor.{_P}_weather_compensation_enabled"
WC_MIN = f"sensor.{_P}_weather_compensation_minimum_temperature"
WC_MAX = f"sensor.{_P}_weather_compensation_maximum_temperature"
FIXED = f"sensor.{_P}_fixed_target_flow_temperature"

BASE_CONFIG = {"heat_pump_id": "test"}

# Keep the app's log output out of the test run (assertLogs still works).
_app_log = logging.getLogger("pyscript.cosy_wc_tuner")
_app_log.addHandler(logging.NullHandler())
_app_log.propagate = False


class FakeState:
    def __init__(self, value, attributes=None):
        self.state = value
        self.attributes = attributes or {}


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data=None, blocking=False, target=None):
        self.calls.append({"domain": domain, "service": service, "data": data or {}, "blocking": blocking})

    def curve_sets(self):
        return [c["data"] for c in self.calls
                if (c["domain"], c["service"]) == ("octopus_energy", "set_heat_pump_flow_temp_config")]

    def notifications(self):
        return [c for c in self.calls if c["domain"] == "notify"]


class App:
    def __init__(self, config=None):
        cfg = dict(BASE_CONFIG)
        cfg.update(config or {})
        for key in [k for k, v in cfg.items() if v is None]:
            del cfg[key]

        self.states = {}
        self.state_sets = []
        self.triggers = []
        self.service_funcs = []
        self.services = FakeServices()
        self.history = {}
        self.history_calls = []
        self.now = NOW

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/London"),
            states=types.SimpleNamespace(get=self._get_state),
            services=self.services,
        )
        g = {
            "__name__": "cosy_wc_tuner",
            "pyscript": types.SimpleNamespace(app_config=cfg, config={}),
            "hass": hass,
            "state": types.SimpleNamespace(set=self._set_state),
            "task": types.SimpleNamespace(unique=lambda name, kill_me=False: None),
            "service": self._service,
            "time_trigger": self._trigger("time_trigger"),
            "state_trigger": self._trigger("state_trigger"),
        }
        source = APP_PATH.read_text()
        exec(compile(source, str(APP_PATH), "exec"), g)
        self.real_fetch_history = g["fetch_history"]

        async def fake_fetch(entity_ids, start, end):
            self.history_calls.append((list(entity_ids), start, end))
            return {k: v for k, v in self.history.items() if k in entity_ids}

        g["fetch_history"] = fake_fetch
        g["now_utc"] = lambda: self.now
        self.g = g
        self.set_curve(35, 50)
        self.states[WC_ENABLED] = ("on", {})
        self.states[FIXED] = ("45", {})
        self.states[POWER] = ("0.8", {"unit_of_measurement": "kW"})

    # --- pyscript / HA fakes ---
    def _get_state(self, entity_id):
        if entity_id in self.states:
            value, attrs = self.states[entity_id]
            return FakeState(value, attrs)
        return None

    def _set_state(self, entity_id, value, new_attributes=None):
        self.state_sets.append((entity_id, value, new_attributes or {}))

    def _service(self, func):
        self.service_funcs.append(func.__name__)
        return func

    def _trigger(self, kind):
        def decorator_factory(*args, **kwargs):
            def decorator(func):
                self.triggers.append((kind, args, func.__name__))
                return func
            return decorator
        return decorator_factory

    # --- helpers for tests ---
    def __getattr__(self, name):
        g = self.__dict__.get("g")
        if g is not None and name in g:
            return g[name]
        raise AttributeError(name)

    def set_curve(self, wc_min, wc_max):
        self.states[WC_MIN] = (str(wc_min), {})
        self.states[WC_MAX] = (str(wc_max), {})

    def run(self, coro):
        return asyncio.run(coro)

    def tune(self, reason="test", allow_apply=True, force=False):
        return self.run(self.g["tune"](reason, allow_apply, force))

    def status(self):
        sets = [s for s in self.state_sets if s[0] == "sensor.cosy_wc_tuner"]
        return sets[-1] if sets else None


# -----------------------------
# Generated history
# -----------------------------
def climate_rows(start, end, err=0.0, target=20.0, mode="heat", every_min=5, target_fn=None):
    """Climate entity rows every few minutes; room = target + err (attributes, like HA)."""
    rows = []
    t = start
    while t < end:
        tg = target_fn(t) if target_fn else target
        rows.append({"t": t, "state": mode,
                     "attributes": {"temperature": tg, "current_temperature": round(tg + err, 2)}})
        t += timedelta(minutes=every_min)
    return rows


def constant_rows(start, value):
    return [{"t": start, "state": str(value), "attributes": {}}]


def power_rows(start, end, duty=1.0, on=0.8, off=0.02):
    """Compressor on for the first `duty` share of each hour."""
    rows = []
    t = start
    on_slots = round(12 * duty)
    while t < end:
        slot = (t.minute // 5) % 12
        rows.append({"t": t, "state": str(on if slot < on_slots else off), "attributes": {}})
        t += timedelta(minutes=5)
    return rows


def curve_rows(start, values):
    """[(hours after start, value), ...] -> WC sensor rows."""
    return [{"t": start + timedelta(hours=h), "state": str(v), "attributes": {}} for h, v in values]


def full_history(err=0.0, outdoor=6.0, duty=1.0, hours=48, now=NOW, wc_min=None, wc_max=None, **kw):
    start = now - timedelta(hours=hours)
    return {
        CLIMATE: climate_rows(start, now, err=err, **kw),
        OUTDOOR: constant_rows(start, outdoor),
        POWER: power_rows(start, now, duty=duty),
        WC_MIN: wc_min if wc_min is not None else constant_rows(start, 35),
        WC_MAX: wc_max if wc_max is not None else constant_rows(start, 50),
    }
