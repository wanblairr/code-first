import copy
import json
import math
import random
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from cafe_core import CafeError
from cafe_forecast_context import (FEATURES, KL, _training_examples, forecast_origin,
                                   make_demo_context_data, predict, train, validate_context,
                                   validate_environment)


def fixture(days=220, flat=False):
    """Synthetic unit-test scenarios; these flags are not official KL holidays."""
    rng=random.Random(72019)
    sales,env=[],[]
    for i in range(days):
        day=date(2026,1,1)+timedelta(days=i)
        temp=26+rng.random()*7
        rain=rng.random()*30
        holiday=i%19==0
        units=100 if flat else round(68+i*.03+day.weekday()*2+2*(temp-29)-1.2*rain+18*holiday-.1*rain*holiday+rng.gauss(0,.5),3)
        sales.append({"date":day.isoformat(),"sku":"test_coffee","units":units,
                      "stockout":False,"closed":False,"data_source":"synthetic"})
        env.append({"date":day.isoformat(),"temperature_c":temp,"precipitation_mm":rain,
                    "is_holiday":holiday,"is_workday_override":False,
                    "known_at":datetime.combine(day-timedelta(days=1),time(18),KL).isoformat(),
                    "weather_kind":"synthetic_forecast","source":"unit-test synthetic calendar"})
    target=date.fromisoformat(sales[-1]["date"])+timedelta(days=1)
    context={"date":target.isoformat(),"temperature_c":32.0,"precipitation_mm":25.0,
             "is_holiday":True,"is_workday_override":False,
             "known_at":datetime.combine(target-timedelta(days=1),time(18),KL).isoformat(),
             "weather_kind":"synthetic_forecast","source":"unit-test synthetic calendar"}
    return sales,env,context


class ContextForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sales,cls.env,cls.context=fixture()
        cls.model=train(cls.sales,cls.env)

    def test_real_fitted_covariates_and_validation_selection(self):
        spec=self.model["models"]["test_coffee"]
        self.assertTrue(spec["algorithm"].startswith("context:"))
        self.assertEqual(len(spec["fit"]["weights"]),len(FEATURES))
        self.assertTrue(all(math.isfinite(w) for w in spec["fit"]["weights"]))
        self.assertGreater(spec["reference_precipitation_mm"],0)
        result=self.model["evaluation"]["test_coffee"]
        self.assertEqual(len(result["validation_predictions"]),28)
        self.assertEqual(len(result["holdout_predictions"]),56)
        self.assertLess(result["validation_predictions"][-1]["date"],result["holdout_start"])
        self.assertIn("previous_observed",result["holdout_metrics"])
        self.assertIn("weekday_mean",result["holdout_metrics"])
        self.assertIn(result["best_without_context_algorithm"],result["holdout_metrics"])
        self.assertIn(result["best_context_algorithm"],result["holdout_metrics"])

    def test_holdout_labels_cannot_select_algorithm(self):
        changed=copy.deepcopy(self.sales)
        cutoff=self.model["evaluation"]["test_coffee"]["holdout_start"]
        for row in changed:
            if row["date"]>=cutoff:
                row["units"]=row["units"]*2+100
        other=train(changed,self.env)["evaluation"]["test_coffee"]
        original=self.model["evaluation"]["test_coffee"]
        self.assertEqual(original["selected_algorithm"],other["selected_algorithm"])
        self.assertEqual(original["validation_metrics"],other["validation_metrics"])
        self.assertNotEqual(original["holdout_metrics"],other["holdout_metrics"])

    def test_each_backtest_uses_prefix_and_pre_22h_forecast(self):
        result=self.model["evaluation"]["test_coffee"]
        for row in result["validation_predictions"]+result["holdout_predictions"]:
            self.assertLess(row["history_through"],row["date"])
            self.assertLessEqual(datetime.fromisoformat(row["context_known_at"]),datetime.fromisoformat(row["forecast_origin"]))
            self.assertEqual(datetime.fromisoformat(row["forecast_origin"]).hour,22)

    def test_forecast_after_scheduled_origin_rejected_even_before_midnight(self):
        raw=dict(self.env[-1])
        raw["known_at"]=(forecast_origin(raw["date"])+timedelta(hours=1)).isoformat()
        raw["forecast_origin"]=raw["known_at"]
        with self.assertRaisesRegex(CafeError,"未来信息"):
            validate_context(raw,historical=True)

    def test_no_realized_weather_imputation_or_bad_types(self):
        for key,value in (("weather_kind","realized_weather"),("temperature_c",float("nan")),
                          ("precipitation_mm",None),("is_holiday",None),("is_holiday",1),
                          ("temperature_c",True)):
            with self.subTest(key=key,value=value),self.assertRaises(CafeError):
                validate_context(dict(self.context,**{key:value}))
        with self.assertRaises(CafeError):
            validate_environment(self.env+[self.env[-1]])
        with self.assertRaises(CafeError):
            train(self.sales,self.env[:-1])

    def test_stale_forecast_future_sales_and_corrupt_model_fail(self):
        old=datetime.fromisoformat(self.context["known_at"])-timedelta(days=3)
        with self.assertRaisesRegex(CafeError,"48"):
            predict(self.model,self.sales,dict(self.context,known_at=old.isoformat()))
        with self.assertRaises(CafeError):
            predict(self.model,self.sales,dict(self.context,date=self.sales[-1]["date"]))
        corrupt=copy.deepcopy(self.model)
        corrupt["models"]["test_coffee"]["fit"]["weights"][0]+=1
        with self.assertRaisesRegex(CafeError,"完整性"):
            predict(corrupt,self.sales,self.context)

    def test_counterfactual_shapley_sums_exactly_and_keeps_histories(self):
        result=predict(self.model,self.sales,self.context)
        row=result["impact"]["items"][0]
        self.assertAlmostEqual(row["baseline_units"]+row["weather_delta_units"]+row["holiday_delta_units"],row["combined_units"],places=6)
        ref=result["counterfactuals"]["reference"]["items"][0]["forecast_units"]
        w=result["counterfactuals"]["weather_only"]["items"][0]["forecast_units"]
        h=result["counterfactuals"]["holiday_only"]["items"][0]["forecast_units"]
        self.assertAlmostEqual(.5*((w-ref)+(row["combined_units"]-h)),row["weather_delta_units"],places=5)
        self.assertLess(row["weather_delta_units"],0)
        self.assertGreater(row["holiday_delta_units"],0)
        changed=predict(self.model,self.sales,dict(self.context,temperature_c=26,precipitation_mm=0))
        self.assertNotEqual(result["items"][0]["forecast_units"],changed["items"][0]["forecast_units"])
        self.assertEqual(result["model_sha256"],changed["model_sha256"])

    def test_censored_units_are_not_label_or_lag_features(self):
        original=copy.deepcopy(self.sales)
        original[40]["stockout"]=True
        changed=copy.deepcopy(original)
        changed[40]["units"]=99999
        env=validate_environment(self.env)
        self.assertEqual(_training_examples(original,env),_training_examples(changed,env))

    def test_flat_series_selects_baseline_and_has_no_fabricated_impact(self):
        sales,env,context=fixture(flat=True)
        model=train(sales,env)
        result=predict(model,sales,context)
        self.assertEqual(model["models"]["test_coffee"]["algorithm"],"previous_observed")
        impact=result["impact"]["items"][0]
        self.assertFalse(impact["context_used"])
        self.assertEqual((impact["weather_delta_units"],impact["holiday_delta_units"]),(0,0))
        self.assertTrue(result["warnings"])

    def test_metadata_is_preserved_not_used_as_a_feature(self):
        context=dict(self.context,holiday_source="official-reference",fetched_at="not-a-feature",holiday_verified=True)
        result=predict(self.model,self.sales,context)
        self.assertEqual(result["context"]["fetched_at"],"not-a-feature")
        self.assertEqual(result["items"],predict(self.model,self.sales,self.context)["items"])

    def test_generated_calendar_and_weather_are_point_in_time_and_transparent(self):
        with tempfile.TemporaryDirectory() as folder:
            sales,env,context=make_demo_context_data(folder)
            manifest=json.loads((Path(folder)/"data_manifest.json").read_text(encoding="utf-8"))
            dates={row["date"] for row in sales}
            self.assertNotIn("2026-02-03",dates)
            self.assertNotIn("2026-03-20",dates)
            self.assertEqual({row["date"] for row in env},dates)
            self.assertEqual(manifest["days"],293)
            self.assertFalse(manifest["contains_real_business_records"])
            self.assertTrue(next(row for row in env if row["date"]=="2026-08-31")["is_holiday"])
            self.assertEqual({row["weather_kind"] for row in env},{"synthetic_forecast"})
            self.assertNotIn("realized_temperature_c",env[0])
            self.assertEqual(context["date"],"2026-10-23")
            noon=datetime(2026,10,22,12,tzinfo=KL).isoformat()
            validate_context(dict(context,forecast_origin=noon))
            self.assertEqual(json.loads((Path(folder)/"target_context.json").read_text(encoding="utf-8")),context)

    def test_future_training_context_cannot_be_mixed_with_tomorrow_scenario(self):
        with self.assertRaisesRegex(CafeError,"未来目标"):
            train(self.sales,self.env+[self.context])


if __name__=="__main__":
    unittest.main()
