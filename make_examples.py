"""Reproducible, explicitly synthetic fixtures. NEVER evidence of real forecasting accuracy."""
from datetime import date, timedelta
import math
import random
from pathlib import Path
from cafe_core import save_json
from cafe_io import demo_context, safe_csv, write_template


def generate(base):
    rng = random.Random(20261021)
    first = date(2026, 7, 1)
    all_rows = []
    for i in range(112):
        d = first + timedelta(days=i)
        weather = "模拟：下雨" if i % 5 == 0 else "模拟：晴"
        event = "模拟：附近校园活动，规模未知" if i % 11 == 0 else "模拟：无已确认活动"
        # These arbitrary coefficients create TEST DATA, not a claim about actual cafes.
        cups = max(0, round(90 + 16*(d.weekday() >= 5) - 9*(i % 5 == 0) + 20*(i % 11 == 0) + 5*math.sin(i/5) + rng.randint(-8, 8)))
        all_rows.append({"date": d.isoformat(), "cups": cups, "weather": weather, "is_holiday": False,
                         "event": event, "stockout": False, "data_source": "synthetic"})
    c = demo_context("2026-10-21")
    history = [r for r in all_rows if r["date"] <= c["sales_date"]][-28:]
    history[-1]["cups"] = c["cups_sold"]
    save_json(base / "examples" / "input.json", {"context": c, "history": history})
    write_template(base / "examples" / "cafe_input.xlsx", c, history)
    safe_csv(base / "examples" / "history.csv", history)
    cases = []
    for index in range(35, 75):
        target = all_rows[index]
        ref = all_rows[index-2]
        context = demo_context((date.fromisoformat(target["date"])-timedelta(days=1)).isoformat())
        context.update(cups_sold=ref["cups"], weather=target["weather"], nearby_event=target["event"], opening_stock_kg=0,
                       safety_stock_kg=0, owner_comment="模拟：没有额外判断", max_order_kg=20)
        cases.append({"context": context, "history": all_rows[max(0,index-29):index-1], "actual_cups": target["cups"]})
    save_json(base / "examples" / "synthetic_backtest_cases.json", cases)
    save_json(base / "examples" / "data_manifest.json", {
        "source": "team_created_synthetic", "seed": 20261021, "generator": "make_examples.py",
        "contains_real_business_records": False, "forecast_accuracy_claim": None,
        "note": "人为规律和随机扰动仅用于工程测试。不得把模型在这些案例的效果称为真实店铺验证。"})


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent)
