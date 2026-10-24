"""Train and export the public, browser-only demonstration without runtime access.

The generator, fitted model and temporal backtest are all synthetic. The Python
training code is shared with the local application, but no application runtime,
credentials, database, local purchase records or private sales are read.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

SITE = Path(__file__).resolve().parent
ROOT = SITE.parents[1]
APP = ROOT / "outputs/Cafe_AI_Replenishment_v3_3"
sys.path.insert(0, str(APP))

from cafe_forecast_context import train, _features, _estimate
from cafe_forecast import _baseline
from cafe_materials import plan

SEED = 2026102303
KL = timezone(timedelta(hours=8))
END = date(2026, 10, 22)


class Random:
    def __init__(self, seed=SEED):
        self.value = seed & 0xffffffff

    def uniform(self):
        x = self.value
        x ^= (x << 13) & 0xffffffff
        x ^= x >> 17
        x ^= (x << 5) & 0xffffffff
        self.value = x & 0xffffffff
        return self.value / 4294967296

    def noise(self):
        return sum(self.uniform() for unused in range(6)) - 3


def holiday_for(day, calendar):
    if not calendar["coverage_start"] <= day <= calendar["coverage_end"]:
        return None
    matching = next((r for r in calendar["holidays"] if r["date"] == day), None)
    return matching["is_holiday"] if matching else False


def generate(end, calendar, days=180):
    """Deterministic, independent scenario labels; not fitted model predictions."""
    start = end - timedelta(days=days - 1)
    rng = Random()
    sales, env = [], []
    weekly = (.92, .96, .99, 1.03, 1.12, 1.32, 1.20)
    for index in range(days):
        day = start + timedelta(days=index)
        holiday = holiday_for(day.isoformat(), calendar)
        # Unknown historic calendar days remain a synthetic ordinary-day
        # generator assumption. They are not published as verified holidays.
        h = holiday is True
        temperature = round(26 + rng.uniform() * 7, 3)
        rain = round(0 if rng.uniform() < .42 else rng.uniform() * 32, 3)
        known = datetime.combine(day - timedelta(days=1), time(18), KL).isoformat()
        env.append({"date": day.isoformat(), "temperature_c": temperature,
                    "precipitation_mm": rain, "is_holiday": h,
                    "is_workday_override": False, "known_at": known,
                    "weather_kind": "synthetic_forecast"})
        latent_temperature = temperature + rng.noise() * .7
        latent_rain = max(0, rain + rng.noise() * 2.5)
        for sku, base, slope, thermal, wet, boost in (
            ("espresso", 58, .035, .8, -.70, 14),
            ("latte", 76, .045, 1.3, -.95, 20),
            ("oat_latte", 34, .065, 1.6, -.60, 12),
        ):
            demand = ((base + slope * index) * weekly[day.weekday()]
                      + thermal * (latent_temperature - 29) + wet * latent_rain
                      + (boost - .15 * latent_rain if h else 0) + rng.noise() * 4.2)
            stockout = index % 67 == 41 and sku == "oat_latte"
            units = max(0, int(demand + .5))
            if stockout:
                units = int(units * .65 + .5)
            sales.append({"date": day.isoformat(), "sku": sku, "units": units,
                          "stockout": stockout, "closed": False, "data_source": "synthetic"})
    return sales, env


def predict_reference(model, calendar, target_date, temperature, rain, is_holiday):
    target = date.fromisoformat(target_date)
    sales, unused = generate(target - timedelta(days=1), calendar)
    context = {"temperature_c": temperature, "precipitation_mm": rain,
               "is_holiday": is_holiday, "is_workday_override": False}
    items, impacts = [], []
    for sku, spec in model["models"].items():
        series = [row for row in sales if row["sku"] == sku]
        origin = date.fromisoformat(series[0]["date"])
        reference = dict(context, temperature_c=spec["reference_temperature_c"],
                         precipitation_mm=spec["reference_precipitation_mm"], is_holiday=False)
        weather_only = dict(reference, temperature_c=temperature, precipitation_mm=rain)
        holiday_only = dict(reference, is_holiday=is_holiday)
        def estimate(c):
            kind = spec["algorithm"]
            if kind.startswith(("context:", "legacy:")):
                return _estimate(spec["fit"], _features(series, target, origin, c, kind.startswith("context:")))
            return _baseline(series, target, kind)
        ref, w, h, combined = [estimate(c) for c in (reference, weather_only, holiday_only, context)]
        wd = round(.5 * ((w - ref) + (combined - h)), 6)
        ref, combined = round(ref, 6), round(combined, 6)
        hd = round(combined - ref - wd, 6)
        items.append({"sku": sku, "forecast_units": combined})
        impacts.append({"sku": sku, "reference": ref, "weather": wd, "holiday": hd, "combined": combined})
    return {"target_date": target_date, "items": items}, impacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", default=END.isoformat())
    args = parser.parse_args()
    end = date.fromisoformat(args.end_date)
    calendar = json.loads((APP / "data/calendar_kl_2026.json").read_text(encoding="utf-8"))
    if not calendar["coverage_start"] <= (end - timedelta(days=179)).isoformat() <= end.isoformat() <= calendar["coverage_end"]:
        raise SystemExit("Training window must be inside the verified 2026 calendar.")
    sales, environment = generate(end, calendar)
    if any(holiday_for(row["date"], calendar) is None for row in environment):
        raise SystemExit("Training dates contain an unknown holiday; choose another window.")
    model = train(sales, environment)
    summary = {
        "data_source": "synthetic", "training_days": 180, "sales_rows": len(sales),
        "generator": "public_demo_training.generate", "seed": SEED,
        "training_start": (end - timedelta(days=179)).isoformat(), "training_end": end.isoformat(),
        "validation_days": 28, "holdout_days": 56,
        "evaluation_method": "chronological rolling one-step; final 56 days excluded from model selection",
        "model_family": "ridge regression with validation-selected baseline fallback",
        "browser_history_policy": "deterministic synthetic 180-day sequence regenerated relative to the selected target; no real business records",
        "contains_real_sales": False, "contains_private_runtime": False,
        "forecast_unit": "drink portions, not unique customers", "sku_metrics": {},
        "limitation": "Synthetic backtest only; not validated for a real cafe and no LLM weights were trained.",
    }
    for sku, evaluation in model["evaluation"].items():
        chosen = evaluation["selected_algorithm"]
        summary["sku_metrics"][sku] = {"algorithm": chosen, "holdout": evaluation["holdout_metrics"][chosen],
                                          "validation": evaluation["validation_metrics"][chosen],
                                          "excluded_stockout_labels": evaluation["excluded_stockout_labels"]}
    inventory = json.loads((APP / "examples_v3/inventory.json").read_text(encoding="utf-8"))
    recipes = json.loads((APP / "examples_v3/recipes.json").read_text(encoding="utf-8"))
    suppliers = json.loads((APP / "examples_v3/suppliers.json").read_text(encoding="utf-8"))
    stock = []
    for item in inventory["items"]:
        stock.append({k: item[k] for k in ("ingredient_id", "name", "unit", "safety_stock", "pack_size", "purchase_unit", "min_order", "max_order", "storage_capacity", "supplier_id")} | {
            "initial_stock": sum(b["quantity"] for b in item["batches"]),
            "due_inbound": sum(i["quantity"] for i in item["inbounds"]),
        })
    exported = {"schemaVersion": 1, "dataSource": "synthetic", "version": "cafe-browser-demo-v1",
                "training": summary, "seed": SEED, "models": model["models"], "features": model["features"],
                "calendar": calendar, "inventory": stock, "recipes": recipes["recipes"],
                "suppliers": [{k: s[k] for k in ("supplier_id", "name", "email")} for s in suppliers["suppliers"]]}
    serialized = json.dumps(exported, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    exported["publicArtifactSha256"] = hashlib.sha256(serialized.encode()).hexdigest()
    output = SITE / "dist/demo"
    output.mkdir(parents=True, exist_ok=True)
    (output / "model.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "training-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    vectors = []
    for target_date, temperature, rain, holiday in [
        ("2026-10-23", 26.5, 22.2, False), ("2026-10-24", 31, 0, False),
        ("2026-11-08", 29, 12, True), ("2026-11-09", 27, 32, True),
        ("2026-12-25", 30, 0, True), ("2026-10-23", 32, 0, True),
    ]:
        forecast, impacts = predict_reference(model, calendar, target_date, temperature, rain, holiday)
        inv, sup = deepcopy(inventory), deepcopy(suppliers)
        inv["as_of_date"] = (date.fromisoformat(target_date) - timedelta(days=1)).isoformat()
        for item in inv["items"]:
            for batch in item["batches"]:
                batch["usable_through"] = (date.fromisoformat(target_date) + timedelta(days=10)).isoformat()
            for inbound in item["inbounds"]:
                inbound["arrival_date"] = target_date
        for supplier in sup["suppliers"]:
            supplier["delivery_date"] = target_date
        procurement = plan(forecast, inv, recipes, sup, target_date)
        vectors.append({"input": {"targetDate": target_date, "weather": {"temperatureC": temperature, "precipitationMm": rain, "source": "simulation"},
                                   "holidayOverride": "public_holiday" if holiday else "ordinary"},
                        "expected_forecasts": forecast["items"], "expected_impacts": impacts,
                        "expected_lines": [{k: r[k] for k in ("ingredient_id", "forecast_usage", "shortage", "suggested_quantity", "pack_size")} for r in procurement["items"]]})
    work = SITE / "work"
    work.mkdir(exist_ok=True)
    (work / "public-demo-parity-vectors.json").write_text(json.dumps(vectors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exported": True, "days": 180, "rows": len(sales), "holdout_days": 56,
                      "metrics": summary["sku_metrics"], "parity_vectors": len(vectors), "private_runtime_read": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
