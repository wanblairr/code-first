"""Chronological evaluation and optional held-out bias calibration (not LLM fine-tuning)."""
from __future__ import annotations
import statistics
from datetime import date
from cafe_core import CafeError, PROMPT_VERSION, fingerprint, number, run_forecast, validate_context, validate_history


def metrics(actual, predicted):
    if not actual or len(actual) != len(predicted):
        raise CafeError("评估数据为空或长度不一致。")
    errors = [p-a for a, p in zip(actual, predicted)]
    return {"n": len(actual), "mae_cups": statistics.mean(abs(e) for e in errors),
            "bias_cups": statistics.mean(errors),
            "wape": sum(abs(e) for e in errors) / sum(actual) if sum(actual) else None,
            "underestimated_cups_total": sum(max(0, -e) for e in errors),
            "overestimated_cups_total": sum(max(0, e) for e in errors)}


def backtest(cases, client, progress=None):
    if not isinstance(cases, list) or not cases:
        raise CafeError("回测需要案例列表。")
    rows, failed, seen = [], [], set()
    for case in sorted(cases, key=lambda x: x["context"]["target_date"]):
        if set(case) != {"context", "history", "actual_cups"}:
            raise CafeError("回测案例需要 context、history、actual_cups。")
        c = validate_context(case["context"])
        history = validate_history(case["history"], c)
        if (c["shop_id"], c["target_date"]) in seen:
            raise CafeError("同店同日回测案例重复。")
        seen.add((c["shop_id"], c["target_date"]))
        actual = number(case["actual_cups"], "实际杯数", 0, c["daily_capacity"])
        # actual_cups never enters run_forecast, evidence, system prompt or client.
        try:
            result = run_forecast(c, history, client, progress)
        except CafeError as error:
            failed.append({"date": c["target_date"], "error": str(error)})
            continue
        if "summary" not in result or result["status"] in ("cancelled", "closed"):
            failed.append({"date": c["target_date"], "error": "未获得完整的十轮估计"})
            continue
        target_weekday = date.fromisoformat(c["target_date"]).weekday()
        same = [r["cups"] for r in history if date.fromisoformat(r["date"]).weekday() == target_weekday and not r["stockout"]][-4:]
        rows.append({"target_date": c["target_date"], "shop_id": c["shop_id"], "raw_forecast_cups": result["summary"]["forecast_cups"],
                     "actual_cups": actual, "naive_cups": c["cups_sold"],
                     "weekday_baseline_cups": statistics.mean(same) if same else c["cups_sold"],
                     "model": result["model"], "run_id": result["run_id"], "data_source": "synthetic",
                     "relative_spread": result["summary"]["relative_spread"], "elapsed_seconds": result.get("elapsed_seconds")})
    report = {"kind": "chronological_synthetic_backtest", "cases": len(cases), "successful": len(rows),
              "coverage": len(rows)/len(cases), "rows": rows, "failed": failed,
              "limitation": "合成案例仅验证方法与流程，不能证明对真实咖啡店有效；模型采样范围不是置信区间。"}
    if rows:
        actual = [r["actual_cups"] for r in rows]
        report["metrics"] = {key: metrics(actual, [r[field] for r in rows]) for key, field in
                             (("llm", "raw_forecast_cups"), ("yesterday_baseline", "naive_cups"), ("weekday_baseline", "weekday_baseline_cups"))}
        m = report["metrics"]
        report["experimental_gate_passed"] = (len(rows) >= 14 and report["coverage"] >= 0.9 and
            m["llm"]["mae_cups"] <= 0.9 * min(m["yesterday_baseline"]["mae_cups"], m["weekday_baseline"]["mae_cups"]))
    else:
        report["experimental_gate_passed"] = False
    report["gate_definition"] = "团队自行设定实验目标：至少14案例、完整输出率≥90%、MAE较两个基线均低至少10%；非赛事官方标准或生产认证。"
    return report


def calibrate(report):
    rows = sorted(report.get("rows", []), key=lambda r: r["target_date"])
    if len(rows) < 30 or report.get("coverage", 0) < 0.9:
        raise CafeError("校准需要至少 30 个完整的历史预测/结果配对，且回测完整输出率至少 90%。")
    signature = fingerprint(rows[0]["model"])
    shops = {r["shop_id"] for r in rows}
    dates = [r["target_date"] for r in rows]
    if len(shops) != 1 or len(set(dates)) != len(dates):
        raise CafeError("校准只支持同一店铺，每日一个记录。")
    for r in rows:
        if r["data_source"] != "synthetic" or r["model"].get("engine") != "ollama" or fingerprint(r["model"]) != signature:
            raise CafeError("校准记录必须来自同一真实 Ollama 模型/提示词配置，数据为模拟标签；测试替身不能训练线上校准。")
        for key in ("raw_forecast_cups", "actual_cups", "naive_cups", "weekday_baseline_cups"):
            number(r[key], key, 0, 10000)
    split = int(len(rows)*0.6)
    train, holdout = rows[:split], rows[split:]
    offset = statistics.mean(r["actual_cups"]-r["raw_forecast_cups"] for r in train)
    actual = [r["actual_cups"] for r in holdout]
    raw = metrics(actual, [r["raw_forecast_cups"] for r in holdout])
    adjusted = metrics(actual, [max(0, r["raw_forecast_cups"]+offset) for r in holdout])
    naive = metrics(actual, [r["naive_cups"] for r in holdout])
    weekday = metrics(actual, [r["weekday_baseline_cups"] for r in holdout])
    passed = adjusted["mae_cups"] < 0.9 * min(raw["mae_cups"], naive["mae_cups"], weekday["mae_cups"])
    return {"kind": "experimental_bias_calibration", "approved_for_experiment": passed, "offset_cups": offset,
            "shop_id": rows[0]["shop_id"], "model": rows[0]["model"], "data_source": "synthetic",
            "train_n": len(train), "holdout_n": len(holdout), "train_end": train[-1]["target_date"], "holdout_end": holdout[-1]["target_date"],
            "holdout_metrics": {"raw": raw, "adjusted": adjusted, "yesterday": naive, "weekday": weekday},
            "source_hash": fingerprint(rows),
            "limitation": "只拟合训练段的平均预测偏差，并在后40%日期检验；没有训练 Llama 权重。一次留出测试不是生产效果保证。"}


def apply_calibration(result, calibration):
    from cafe_core import procurement
    if result["status"] not in ("needs_review", "blocked") or "summary" not in result:
        raise CafeError("没有完整预测可校准。")
    c = result["context"]
    if (calibration.get("kind") != "experimental_bias_calibration" or calibration.get("approved_for_experiment") is not True
        or calibration.get("data_source") != "synthetic" or calibration.get("shop_id") != c["shop_id"]
        or fingerprint(calibration.get("model")) != fingerprint(result["model"])):
        raise CafeError("校准未通过留出验证，或店铺/模型/版本不匹配。")
    if date.fromisoformat(c["target_date"]) <= date.fromisoformat(calibration["holdout_end"]):
        raise CafeError("校准只能用于留出集结束之后的新日期，防止数据泄漏。")
    offset = number(calibration["offset_cups"], "校准偏差", -10000, 10000)
    raw = result["summary"]["forecast_cups"]
    adjusted = max(0, raw+offset)
    if adjusted > c["daily_capacity"]:
        raise CafeError("校准后超过产能；不能强行截断后批准。")
    result["summary"]["raw_forecast_cups"] = raw
    result["summary"]["forecast_cups"] = adjusted
    result["calibration"] = calibration
    result["procurement"] = procurement(c, adjusted)
    result["status"] = "blocked" if result["procurement"]["blockers"] else "needs_review"
    result["warnings"].append("已使用明确选择的实验误差校准；采样范围仍是校准前的模型分歧，不是置信区间。")
    return result
