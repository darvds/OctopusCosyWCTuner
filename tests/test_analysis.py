import asyncio
import sys
import types
import unittest
from datetime import timedelta

from tests.harness import (App, NOW, CLIMATE, OUTDOOR, POWER, WC_MIN, WC_MAX,
                           full_history, constant_rows, power_rows)


def analyse(app, hist, hours=24, scale=1.0):
    return app.analyse(hist, NOW - timedelta(hours=hours), NOW, scale)


class AnalysisTests(unittest.TestCase):
    def test_error_split_evenly_mid_curve(self):
        # 6C is half way between -3C and 15C, so each end gets half the data.
        app = App()
        s_min, s_max, summary = analyse(app, full_history(err=-0.5, outdoor=6))
        self.assertEqual(summary, {"samples": 288, "outdoor_mean": 6.0})
        for s in (s_min, s_max):
            self.assertEqual(s, {"hours": 12.0, "share": 0.5, "err": -0.5, "duty": 1.0})

    def test_mild_weather_only_counts_for_min_end(self):
        app = App()
        s_min, s_max, _ = analyse(app, full_history(err=0.4, outdoor=15))
        self.assertEqual(s_min["hours"], 24.0)
        self.assertEqual(s_min["share"], 1.0)
        self.assertEqual(s_max, {"hours": 0.0, "share": 0.0, "err": None, "duty": None})

    def test_cold_weather_only_counts_for_max_end(self):
        app = App()
        s_min, s_max, _ = analyse(app, full_history(err=0.0, outdoor=-5))
        self.assertEqual(s_max["share"], 1.0)
        self.assertIsNone(s_min["err"])

    def test_setback_and_recovery_excluded(self):
        # 17C setback until 12 h ago, then 20C: the 2 h warm-up is skipped too.
        app = App()
        rise = NOW - timedelta(hours=12)
        hist = full_history(err=0.0, target_fn=lambda t: 17.0 if t < rise else 20.0)
        _, _, summary = analyse(app, hist)
        self.assertEqual(summary["samples"], 12 * 12 - 2 * 12)

    def test_off_mode_excluded(self):
        app = App()
        _, _, summary = analyse(app, full_history(mode="off"))
        self.assertEqual(summary["samples"], 0)

    def test_duty_from_kw_sensor(self):
        app = App()
        s_min, _, _ = analyse(app, full_history(duty=0.5))
        self.assertEqual(s_min["duty"], 0.5)

    def test_duty_from_watt_sensor(self):
        app = App()
        app.states[POWER] = ("800", {"unit_of_measurement": "W"})
        hist = full_history()
        hist[POWER] = power_rows(NOW - timedelta(hours=48), NOW, duty=0.5, on=800, off=20)
        s_min, _, _ = analyse(app, hist, scale=app.power_scale())
        self.assertEqual(app.power_scale(), 0.001)
        self.assertEqual(s_min["duty"], 0.5)

    def test_room_sensor_override(self):
        app = App({"room_temp_sensor": "sensor.lounge"})
        hist = full_history(err=0.0)
        hist["sensor.lounge"] = constant_rows(NOW - timedelta(hours=48), 19.0)
        s_min, _, _ = analyse(app, hist)
        self.assertEqual(s_min["err"], -1.0)

    def test_missing_data_skipped(self):
        app = App()
        hist = full_history()
        del hist[OUTDOOR]
        _, _, summary = analyse(app, hist)
        self.assertEqual(summary, {"samples": 0, "outdoor_mean": None})


class LastChangeTests(unittest.TestCase):
    def test_finds_latest_change_on_either_end(self):
        app = App()
        start = NOW - timedelta(hours=48)
        hist = {
            WC_MIN: [{"t": start, "state": "35"}, {"t": start + timedelta(hours=10), "state": "36"}],
            WC_MAX: [{"t": start, "state": "50"}, {"t": start + timedelta(hours=20), "state": "49"}],
        }
        self.assertEqual(app.last_curve_change(hist), start + timedelta(hours=20))

    def test_unavailable_blip_is_not_a_change(self):
        app = App()
        start = NOW - timedelta(hours=48)
        hist = {WC_MIN: [{"t": start, "state": "35"},
                         {"t": start + timedelta(hours=1), "state": "unavailable"},
                         {"t": start + timedelta(hours=2), "state": "35"}]}
        self.assertIsNone(app.last_curve_change(hist))

    def test_no_history(self):
        self.assertIsNone(App().last_curve_change({}))


class FetchHistoryTests(unittest.TestCase):
    """The real fetch_history against a stub recorder."""

    def setUp(self):
        self.calls = []
        recorder = types.ModuleType("homeassistant.components.recorder")
        history = types.ModuleType("homeassistant.components.recorder.history")

        class Instance:
            async def async_add_executor_job(self, job):
                return job()

        recorder.get_instance = lambda hass: Instance()
        old = NOW - timedelta(hours=10)
        new = NOW - timedelta(hours=1)

        def get_significant_states(hass, start, end, entity_ids, **kwargs):
            self.calls.append((start, end, entity_ids, kwargs))
            # An attribute-only update: last_changed stays old, last_updated moves.
            s = types.SimpleNamespace(state="heat", attributes={"current_temperature": 20.5},
                                      last_changed=old, last_updated=new)
            return {CLIMATE: [s]}

        history.get_significant_states = get_significant_states
        self.modules = {
            "homeassistant": types.ModuleType("homeassistant"),
            "homeassistant.components": types.ModuleType("homeassistant.components"),
            "homeassistant.components.recorder": recorder,
            "homeassistant.components.recorder.history": history,
        }
        self.saved = {k: sys.modules.get(k) for k in self.modules}
        sys.modules.update(self.modules)
        self.new = new

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_uses_last_updated_and_all_changes(self):
        app = App()
        out = asyncio.run(app.real_fetch_history([CLIMATE], NOW - timedelta(hours=24), NOW))
        self.assertEqual(out[CLIMATE], [{"t": self.new, "state": "heat",
                                         "attributes": {"current_temperature": 20.5}}])
        kwargs = self.calls[0][3]
        self.assertFalse(kwargs["significant_changes_only"])
        self.assertFalse(kwargs["no_attributes"])


if __name__ == "__main__":
    unittest.main()
