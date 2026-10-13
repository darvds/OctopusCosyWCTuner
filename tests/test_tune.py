import unittest
from datetime import timedelta

from tests.harness import (App, NOW, CLIMATE, WC_ENABLED, WC_MIN, FIXED,
                           full_history, curve_rows)

START = NOW - timedelta(hours=48)


class TuneTests(unittest.TestCase):
    def test_diagnostics_only_proposes_without_writing(self):
        app = App({"diagnostics_only": True})
        app.history = full_history(err=-0.5)
        result = app.tune()
        self.assertEqual(result["status"], "proposed")
        self.assertEqual((result["proposed_min"], result["proposed_max"]), (36, 51))
        self.assertEqual(app.services.curve_sets(), [])
        self.assertEqual(app.status()[1], "proposed")

    def test_applies_curve(self):
        app = App()
        app.history = full_history(err=-0.5)
        result = app.tune()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(app.services.curve_sets(), [{
            "entity_id": CLIMATE,
            "weather_comp_enabled": True,
            "weather_comp_min_temperature": 36,
            "weather_comp_max_temperature": 51,
            "fixed_flow_temperature": 45.0,
        }])
        self.assertTrue(app.services.calls[0]["blocking"])
        self.assertEqual(app.g["_last"]["applied_at"], NOW)

    def test_fixed_flow_falls_back_to_max(self):
        app = App()
        del app.states[FIXED]
        app.history = full_history(err=-0.5)
        app.tune()
        self.assertEqual(app.services.curve_sets()[0]["fixed_flow_temperature"], 50.0)

    def test_mild_weather_only_moves_min_end(self):
        app = App()
        app.history = full_history(err=-0.5, outdoor=15)
        result = app.tune()
        self.assertEqual((result["proposed_min"], result["proposed_max"]), (36, 50))
        self.assertIn("insufficient", result["max_decision"])

    def test_no_change(self):
        app = App()
        app.history = full_history(err=0.0, duty=1.0)
        self.assertEqual(app.tune()["status"], "no_change")
        self.assertEqual(app.services.curve_sets(), [])

    def test_waits_to_settle_after_own_change(self):
        app = App()
        app.history = full_history(err=-0.5)
        app.tune()
        app.now = NOW + timedelta(hours=6)
        self.assertEqual(app.tune()["status"], "waiting_settle")
        self.assertEqual(len(app.services.curve_sets()), 1)

    def test_waits_to_settle_after_change_in_octopus_app(self):
        app = App()
        app.history = full_history(err=-0.5, wc_min=curve_rows(START, [(0, 34), (42, 35)]))
        result = app.tune()
        self.assertEqual(result["status"], "waiting_settle")
        self.assertEqual(result["last_change"], (NOW - timedelta(hours=6)).isoformat())

    def test_window_starts_after_settle(self):
        app = App({"diagnostics_only": True})
        app.history = full_history(err=-0.5, wc_min=curve_rows(START, [(0, 34), (18, 35)]))
        result = app.tune()
        # Changed 30 h ago -> window starts 18 h ago.
        self.assertEqual(result["window_start"], (NOW - timedelta(hours=18)).isoformat())
        self.assertEqual(result["samples"], 18 * 12)

    def test_min_days_between_changes(self):
        app = App()
        app.history = full_history(err=-0.5, wc_min=curve_rows(START, [(0, 34), (18, 35)]))
        result = app.tune()
        self.assertEqual(result["status"], "waiting_min_days")
        self.assertEqual((result["proposed_min"], result["proposed_max"]), (36, 51))
        self.assertEqual(app.services.curve_sets(), [])

    def test_force_skips_min_days(self):
        app = App()
        app.history = full_history(err=-0.5, wc_min=curve_rows(START, [(0, 34), (18, 35)]))
        self.assertEqual(app.tune(force=True)["status"], "applied")

    def test_history_window_covers_min_days(self):
        app = App()
        app.history = full_history()
        app.tune()
        ids, start, end = app.history_calls[0]
        self.assertEqual(end - start, timedelta(days=2))
        self.assertIn(WC_MIN, ids)

    def test_weather_compensation_off(self):
        app = App()
        app.states[WC_ENABLED] = ("off", {})
        self.assertEqual(app.tune()["status"], "skipped")
        self.assertEqual(app.history_calls, [])

    def test_curve_unreadable(self):
        app = App()
        app.states[WC_MIN] = ("unavailable", {})
        self.assertEqual(app.tune()["status"], "skipped")

    def test_preview_never_writes(self):
        app = App()
        app.history = full_history(err=-0.5)
        self.assertEqual(app.tune("preview", allow_apply=False)["status"], "proposed")
        self.assertEqual(app.services.curve_sets(), [])

    def test_notifies_on_apply(self):
        app = App({"notify_service": "notify.mobile_app_phone"})
        app.history = full_history(err=-0.5)
        app.tune()
        notes = app.services.notifications()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["service"], "mobile_app_phone")
        self.assertIn("35/50C -> 36/51C", notes[0]["data"]["message"])

    def test_error_published(self):
        app = App()

        async def broken(*args):
            raise RuntimeError("recorder down")

        app.g["fetch_history"] = broken
        with self.assertLogs("pyscript.cosy_wc_tuner", level="ERROR"):
            result = app.tune()
        self.assertEqual(result["status"], "error")
        self.assertEqual(app.status()[2]["message"], "recorder down")
        self.assertFalse(app.g["_flags"]["busy"])

    def test_status_attributes(self):
        app = App({"diagnostics_only": True})
        app.history = full_history(err=-0.5)
        app.tune("daily")
        entity, value, attrs = app.status()
        self.assertEqual(entity, "sensor.cosy_wc_tuner")
        self.assertEqual(attrs["reason"], "daily")
        self.assertEqual(attrs["current_min"], 35)
        self.assertEqual(attrs["proposed_max"], 51)
        self.assertEqual(attrs["min_end"]["err"], -0.5)
        self.assertIn("raise", attrs["max_decision"])
        self.assertTrue(attrs["diagnostics_only"])
        self.assertEqual(attrs["run_at"], "05:30")


if __name__ == "__main__":
    unittest.main()
