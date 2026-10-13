import unittest

from tests.harness import App, CLIMATE


class TriggerTests(unittest.TestCase):
    def test_registered_triggers_and_services(self):
        app = App()
        kinds = {(t[0], t[1]) for t in app.triggers}
        self.assertEqual(kinds, {("time_trigger", ("startup",)),
                                 ("time_trigger", ("cron(30 5 * * *)",))})
        self.assertEqual(set(app.service_funcs), {"cosy_wc_tuner_run", "cosy_wc_tuner_preview"})

    def test_run_at(self):
        app = App({"run_at": "22:05"})
        self.assertIn(("time_trigger", ("cron(5 22 * * *)",), "cosy_wc_tuner_daily"), app.triggers)

    def test_invalid_run_at(self):
        app = App({"run_at": "soon"})
        self.assertIn(("time_trigger", ("cron(30 5 * * *)",), "cosy_wc_tuner_daily"), app.triggers)

    def test_entity_overrides(self):
        app = App({"climate_entity": "climate.cosy", "heat_pump_id": None})
        self.assertEqual(app.ENT["climate"], "climate.cosy")
        self.assertEqual(app.ENT["outdoor"], "sensor.octopus_energy_heat_pump__live_outdoor_temperature")

    def test_startup_publishes_idle_and_warns(self):
        app = App()
        with self.assertLogs("pyscript.cosy_wc_tuner", level="WARNING") as logs:
            app.run(app.cosy_wc_tuner_startup())
        self.assertTrue(any(CLIMATE in line for line in logs.output))
        self.assertEqual(app.status()[1], "idle")
        self.assertEqual(app.status()[2]["current_max"], 50.0)

    def test_run_service_force(self):
        app = App()
        calls = []

        async def fake_tune(reason, allow_apply=True, force=False):
            calls.append((reason, allow_apply, force))

        app.g["tune"] = fake_tune
        app.run(app.cosy_wc_tuner_run(force="true"))
        app.run(app.cosy_wc_tuner_run())
        app.run(app.cosy_wc_tuner_preview())
        self.assertEqual(calls, [("manual", True, True), ("manual", True, False), ("preview", False, False)])


if __name__ == "__main__":
    unittest.main()
