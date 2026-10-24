"""Forecast leakage, model selection, persistence and input-contract checks."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from cafe_core import CafeError
from cafe_forecast import (FEATURES, _solve, make_demo_sales, predict,
                           read_sales_csv, train, validate_sales)


class ForecastTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.gettempdir()).resolve()
        cls.temp = tempfile.TemporaryDirectory(dir=cls.scratch)
        assert Path(cls.temp.name).resolve().is_relative_to(cls.scratch)
        cls.csv = Path(cls.temp.name)/"sales.csv"
        all_sales = make_demo_sales(cls.csv, days=180)
        cls.sales = [r for r in all_sales if r["sku"] == "oat_latte"]
        cls.model = train(cls.sales, Path(cls.temp.name)/"model.json")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fit_is_actual_persisted_weights(self):
        spec = self.model["models"]["oat_latte"]
        self.assertTrue(spec["algorithm"].startswith("ridge:"))
        self.assertEqual(len(spec["fit"]["weights"]), len(FEATURES))
        self.assertTrue(all(math.isfinite(w) for w in spec["fit"]["weights"]))
        restored = json.loads((Path(self.temp.name)/"model.json").read_text(encoding="utf-8"))
        self.assertEqual(predict(restored, self.sales, "2026-10-23"),
                         predict(self.model, self.sales, "2026-10-23"))

    def test_solver_known_solution(self):
        actual = _solve([[4, 1], [1, 3]], [1, 2])
        self.assertAlmostEqual(actual[0], 1/11)
        self.assertAlmostEqual(actual[1], 7/11)

    def test_future_holdout_cannot_change_model_selection(self):
        modified = copy.deepcopy(self.sales)
        for row in modified[-14:]:
            row["units"] = row["units"]*15+300
        changed = train(modified)
        before, after = self.model["evaluation"]["oat_latte"], changed["evaluation"]["oat_latte"]
        self.assertEqual(before["selected_algorithm"], after["selected_algorithm"])
        self.assertEqual(before["validation_metrics"], after["validation_metrics"])
        self.assertNotEqual(before["holdout_metrics"], after["holdout_metrics"])

    def test_every_backtest_prefix_precedes_target(self):
        report = self.model["evaluation"]["oat_latte"]
        for row in report["validation_predictions"]+report["holdout_predictions"]:
            self.assertLess(row["history_through"], row["date"])

    def test_censored_values_do_not_train_or_leak_into_lags(self):
        changed = copy.deepcopy(self.sales)
        for row in changed:
            if row["stockout"]:
                row["units"] = 9999
        model = train(changed)
        self.assertEqual(self.model["models"], model["models"])

    def test_latest_stockout_warns_and_is_supported(self):
        modified = copy.deepcopy(self.sales)
        modified[-1]["stockout"] = True
        result = predict(self.model, modified, "2026-10-23")
        self.assertTrue(result["warnings"])
        self.assertGreaterEqual(result["items"][0]["forecast_units"], 0)

    def test_synthetic_only_no_future_or_stale_predictions(self):
        cases = []
        real = copy.deepcopy(self.sales)
        real[0]["data_source"] = "real"
        cases.append((real, "2026-10-23"))
        cases += [(self.sales, "2026-10-22"), (self.sales, "2026-10-24"),
                  (self.sales[:-1], "2026-10-23")]
        for sales, target in cases:
            with self.subTest(target=target), self.assertRaises(CafeError):
                predict(self.model, sales, target)

    def test_missing_sku_and_corrupt_model_fail(self):
        bad = copy.deepcopy(self.model)
        bad["models"]["oat_latte"]["fit"]["weights"][0] += 1
        with self.assertRaises(CafeError):
            predict(bad, self.sales, "2026-10-23")
        renamed = [dict(r, sku="other_sku") for r in self.sales]
        with self.assertRaises(CafeError):
            predict(self.model, renamed, "2026-10-23")

    def test_missing_days_are_reported_not_filled_with_zero(self):
        modified = [r for i, r in enumerate(self.sales) if i != 18]
        model = train(modified)
        report = model["evaluation"]["oat_latte"]
        self.assertEqual(report["missing_calendar_days"], 1)
        self.assertEqual(report["records"], 179)

    def test_duplicate_nonfinite_boolean_and_closed_bad_data_fail(self):
        with self.assertRaises(CafeError):
            validate_sales(self.sales+[self.sales[0]])
        for updates in ({"units": float("nan")}, {"units": True}, {"stockout": "False"},
                        {"closed": True}, {"data_source": "real"}):
            with self.subTest(updates=updates), self.assertRaises(CafeError):
                validate_sales([dict(self.sales[0], **updates)])

    def test_csv_roundtrip_and_insufficient_data_fail(self):
        self.assertEqual(len(read_sales_csv(self.csv)), 540)
        with self.assertRaises(CafeError):
            train(self.sales[:40])

    def test_flat_series_falls_back_to_baseline(self):
        flat = [dict(r, units=40.0, stockout=False) for r in self.sales]
        model = train(flat)
        self.assertEqual(model["models"]["oat_latte"]["algorithm"], "previous_observed")
        self.assertEqual(predict(model, flat, "2026-10-23")["items"][0]["forecast_units"], 40)


if __name__ == "__main__":
    unittest.main()
