import unittest

from tests.harness import App


def stats(hours=12.0, share=0.5, err=0.0, duty=1.0):
    return {"hours": hours, "share": share, "err": err, "duty": duty}


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_too_cold_raises(self):
        self.assertEqual(self.app.decide(stats(err=-0.5))[0], 1.0)

    def test_too_warm_lowers(self):
        self.assertEqual(self.app.decide(stats(err=0.5))[0], -1.0)

    def test_on_target_but_cycling_trims(self):
        delta, why = self.app.decide(stats(err=0.0, duty=0.4))
        self.assertEqual(delta, -1.0)
        self.assertIn("trim", why)

    def test_on_target_long_cycles_holds(self):
        self.assertEqual(self.app.decide(stats(err=0.0, duty=0.8))[0], 0.0)

    def test_slightly_cold_and_cycling_holds(self):
        self.assertEqual(self.app.decide(stats(err=-0.2, duty=0.4))[0], 0.0)

    def test_insufficient_hours(self):
        delta, why = self.app.decide(stats(hours=2.0, err=-1.0))
        self.assertEqual(delta, 0.0)
        self.assertIn("insufficient", why)

    def test_no_data(self):
        self.assertEqual(self.app.decide(stats(hours=0.0, share=0.0, err=None, duty=None))[0], 0.0)

    def test_end_not_exercised(self):
        delta, why = self.app.decide(stats(share=0.2, err=-1.0))
        self.assertEqual(delta, 0.0)
        self.assertIn("didn't exercise", why)

    def test_custom_step(self):
        app = App({"step": 0.5})
        self.assertEqual(app.decide(stats(err=-0.5))[0], 0.5)


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def test_moves_both_ends(self):
        self.assertEqual(self.app.propose(35, 50, 1, -1, "a", "b")[:2], (36, 49))

    def test_bounds(self):
        self.assertEqual(self.app.propose(45, 57, 1, 1, "a", "b")[:2], (45, 57))
        self.assertEqual(self.app.propose(30, 40, -1, -1, "a", "b")[:2], (30, 40))

    def test_custom_bounds(self):
        app = App({"min_flow_bounds": [40, 32]})
        self.assertEqual(app.MIN_FLOW_BOUNDS, (32.0, 40.0))
        self.assertEqual(app.propose(40, 50, 1, 0, "a", "b")[:2], (40, 50))

    def test_gap_blocks_min_raise(self):
        new_min, new_max, why_min, _ = self.app.propose(40, 46, 1, 0, "raise", "hold")
        self.assertEqual((new_min, new_max), (40, 46))
        self.assertIn("blocked: min gap", why_min)

    def test_gap_blocks_max_lower(self):
        new_min, new_max, _, why_max = self.app.propose(40, 46, 0, -1, "hold", "lower")
        self.assertEqual((new_min, new_max), (40, 46))
        self.assertIn("blocked: min gap", why_max)

    def test_gap_undoes_min_first(self):
        new_min, new_max, why_min, why_max = self.app.propose(40, 47, 1, -1, "raise", "lower")
        self.assertEqual((new_min, new_max), (40, 46))
        self.assertIn("blocked", why_min)
        self.assertNotIn("blocked", why_max)


if __name__ == "__main__":
    unittest.main()
