"""Fitted weather/calendar demand models for synthetic Kuala Lumpur scenarios.

Historical covariates are day-ahead forecast snapshots known before each 22:00
forecast origin. Realized weather is never substituted into a historical target.
Counterfactual differences describe this model, not a verified causal effect.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from cafe_core import CafeError, save_json
from cafe_forecast import (_baseline, _day, _digest, _features as legacy_features,
                           _metric, _solve, read_sales_csv, validate_sales)

VERSION = "cafe-demand-context-v3.2"
KL = timezone(timedelta(hours=8), "Asia/Kuala_Lumpur")
BASE_FEATURES = ["intercept"] + [f"weekday_{i}" for i in range(1, 7)] + [
    "elapsed_weeks", "previous_observed", "same_weekday_mean_4", "recent_mean_7", "recent_mean_28"]
EXTRA_FEATURES = ["temperature_c", "precipitation_mm", "is_holiday", "is_workday_override",
                  "temperature_x_holiday", "precipitation_x_holiday"]
FEATURES = BASE_FEATURES + EXTRA_FEATURES
ALPHAS = (0.1, 1.0, 10.0)
MIN_ROWS = 140


def forecast_origin(day):
    return datetime.combine(_day(day)-timedelta(days=1), time(22), KL)


def _instant(value, label):
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise CafeError(f"{label}须为含时区的 ISO 时间。") from None


def validate_context(raw, target_date=None, *, historical=False):
    required = {"date", "temperature_c", "precipitation_mm", "is_holiday",
                "is_workday_override", "known_at", "weather_kind"}
    if not isinstance(raw, dict) or required-raw.keys():
        raise CafeError("天气日历需要日期、温度、降水、假日/调休标记、known_at 和 weather_kind。")
    day = _day(raw["date"])
    if target_date is not None and raw["date"] != target_date:
        raise CafeError("天气/假日日期与预测目标不一致。")
    output = dict(raw)
    for key, low, high in (("temperature_c", -20, 55), ("precipitation_mm", 0, 500)):
        val = raw[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not low <= val <= high:
            raise CafeError(f"{key}必须为 {low}–{high} 的有限数字；未知不能当零。")
        output[key] = float(val)
    for key in ("is_holiday", "is_workday_override"):
        if type(raw[key]) is not bool:
            raise CafeError(f"{key}须为已确认布尔值，未知不能当普通工作日。")
    kinds = {"synthetic_forecast"} if historical else {"synthetic_forecast", "live_forecast", "manual_scenario"}
    if raw["weather_kind"] not in kinds:
        raise CafeError("仅使用预测时已知的天气预报快照，不能以事后实况代替；训练只接受模拟预报。")
    known = _instant(raw["known_at"], "known_at")
    origin = forecast_origin(raw["date"]) if historical else _instant(raw.get("forecast_origin", forecast_origin(raw["date"]).isoformat()), "forecast_origin")
    midnight = datetime.combine(day, time.min, KL)
    if origin >= midnight or origin < midnight-timedelta(days=1):
        raise CafeError("预测起点须在目标日前一天，且早于目标日开始。")
    if known > origin:
        raise CafeError("天气快照在当次预测起点之后才可知，存在未来信息泄漏。")
    if raw.get("calendar_known_from") and _instant(raw["calendar_known_from"],"calendar_known_from")>origin:
        raise CafeError("假日信息在当次预测起点之后才可知，存在未来日历泄漏。")
    if origin-known > timedelta(hours=48):
        raise CafeError("天气预报快照超过48小时，请刷新，不能静默使用陈旧天气。")
    output["forecast_origin"] = origin.isoformat()
    return output


def validate_environment(rows):
    if not isinstance(rows, list) or not rows or len(rows)>10000:
        raise CafeError("environment须为非空天气/日历快照列表。")
    result = {}
    for raw in rows:
        row = validate_context(raw, historical=True)
        if row["date"] in result:
            raise CafeError("同一日期天气快照重复；请选择当时可知的唯一版本。")
        result[row["date"]] = row
    return result


def _features(history, target, origin, context, contextual=True):
    base = legacy_features(history, target, origin)
    if not contextual:
        return base
    temp, rain = context["temperature_c"], context["precipitation_mm"]
    holiday = float(context["is_holiday"])
    return base + [temp, rain, holiday, float(context["is_workday_override"]),
                   temp*holiday, rain*holiday]


def _training_examples(series, environment):
    origin = _day(series[0]["date"])
    examples = []
    valid_count = 0
    for i, row in enumerate(series):
        eligible = not row["stockout"] and not row["closed"]
        if eligible and valid_count >= 14:
            context = environment[row["date"]]
            examples.append({"index": i, "x": _features(series[:i], _day(row["date"]), origin, context),
                             "y": row["units"]})
        if eligible:
            valid_count += 1
    return examples


def _moments(examples, n_features):
    n = len(examples)
    if n < 28:
        raise CafeError("至少需要28个有效拟合标签。")
    sx, sxy = [0.0]*n_features, [0.0]*n_features
    sxx = [[0.0]*n_features for _ in range(n_features)]
    sy = 0.0
    for example in examples:
        x, y = example["x"][:n_features], example["y"]
        sy += y
        for j in range(n_features):
            sx[j] += x[j]
            sxy[j] += x[j]*y
            for k in range(j, n_features):
                sxx[j][k] += x[j]*x[k]
    means = [0.0]+[sx[j]/n for j in range(1,n_features)]
    scales = [1.0]+[max(1e-8, math.sqrt(max(0.0, sxx[j][j]/n-means[j]**2))) for j in range(1,n_features)]
    matrix = [[0.0]*n_features for _ in range(n_features)]
    rhs = [sy]+[(sxy[j]-means[j]*sy)/scales[j] for j in range(1,n_features)]
    matrix[0][0] = n
    for j in range(1,n_features):
        for k in range(j,n_features):
            matrix[j][k] = matrix[k][j] = (sxx[j][k]-sx[j]*sx[k]/n)/(scales[j]*scales[k])
    return {"n":n,"matrix":matrix,"rhs":rhs,"means":means,"scales":scales}


def _fit_moments(moments, alpha):
    matrix = [list(r) for r in moments["matrix"]]
    for j in range(1,len(matrix)):
        matrix[j][j] += alpha
    return {"weights":_solve(matrix,moments["rhs"]), "means":moments["means"],
            "scales":moments["scales"], "alpha":alpha, "fitted_rows":moments["n"]}


def _estimate(fit, x):
    result = sum(w*(v-m)/s for w,v,m,s in zip(fit["weights"],x,fit["means"],fit["scales"]))
    if not math.isfinite(result) or result>100000:
        raise CafeError("模型输出异常，请核对输入分布。")
    return max(0.0,result)


def _evaluate(series, examples, env, indices, algorithms):
    actual, estimates, details = [], {a:[] for a in algorithms}, []
    origin = _day(series[0]["date"])
    for i in indices:
        row = series[i]
        if row["stockout"] or row["closed"]:
            continue
        target = _day(row["date"])
        context = env[row["date"]]
        prefix = series[:i]
        past_examples = [e for e in examples if e["index"]<i]
        moments = {}
        predictions = {}
        for algorithm in algorithms:
            if algorithm.startswith(("context:","legacy:")):
                kind, alpha = algorithm.split(":")
                size = len(FEATURES) if kind=="context" else len(BASE_FEATURES)
                if size not in moments:
                    moments[size] = _moments(past_examples,size)
                fit = _fit_moments(moments[size],float(alpha))
                x = _features(prefix,target,origin,context,kind=="context")
                guess = _estimate(fit,x)
            else:
                guess = _baseline(prefix,target,algorithm)
            predictions[algorithm] = round(guess,6)
            estimates[algorithm].append(guess)
        actual.append(row["units"])
        details.append({"date":row["date"],"actual_units":row["units"],
                        "history_through":prefix[-1]["date"],"context_known_at":context["known_at"],
                        "forecast_origin":forecast_origin(row["date"]).isoformat(),
                        "is_holiday":context["is_holiday"],"predictions":predictions})
    if len(actual)<7:
        raise CafeError("验证或留出集有效标签不足7条。")
    return {a:_metric(actual,g) for a,g in estimates.items()}, details


def _slice_metrics(details, algorithms, holiday):
    chosen = [d for d in details if d["is_holiday"] is holiday]
    if not chosen:
        return {"n":0,"note":"该窗口没有此类样本，不估计该分组效果。"}
    return {a:_metric([d["actual_units"] for d in chosen], [d["predictions"][a] for d in chosen]) for a in algorithms}


def train(sales, environment, output_path=None):
    rows, env = validate_sales(sales), validate_environment(environment)
    if any(r["date"] not in env for r in rows):
        raise CafeError("每个销售日期都需要当时已知的天气和日历快照，缺失不能静默补零。")
    if any(day > rows[-1]["date"] for day in env):
        raise CafeError("训练天气中含未来目标日期，请把明日情景与历史训练快照分开。")
    models, report = {}, {}
    for sku in sorted({r["sku"] for r in rows}):
        series = [r for r in rows if r["sku"]==sku]
        if len(series)<MIN_ROWS:
            raise CafeError(f"{sku}至少需要{MIN_ROWS}条日销售记录。")
        end = _day(series[-1]["date"])
        holdout_start, validation_start = end-timedelta(days=55), end-timedelta(days=83)
        validation = [i for i,r in enumerate(series) if validation_start<=_day(r["date"])<holdout_start]
        holdout = [i for i,r in enumerate(series) if _day(r["date"])>=holdout_start]
        examples = _training_examples(series,env)
        algorithms = ["previous_observed","weekday_mean"] + [f"legacy:{a}" for a in ALPHAS] + [f"context:{a}" for a in ALPHAS]
        scores, val_predictions = _evaluate(series,examples,env,validation,algorithms)
        baseline = min(algorithms[:2],key=lambda a:scores[a]["mae_units"])
        legacy = min([a for a in algorithms if a.startswith("legacy:")],key=lambda a:scores[a]["mae_units"])
        contextual = min([a for a in algorithms if a.startswith("context:")],key=lambda a:scores[a]["mae_units"])
        best_without = legacy if scores[legacy]["mae_units"]<scores[baseline]["mae_units"]*.99 else baseline
        chosen = contextual if scores[contextual]["mae_units"]<scores[best_without]["mae_units"]*.99 else best_without
        finalists = list(dict.fromkeys([chosen,baseline,legacy,contextual,"previous_observed","weekday_mean"]))
        held_scores, held_predictions = _evaluate(series,examples,env,holdout,finalists)
        fits = {}
        for algorithm in (chosen,legacy,contextual):
            if algorithm not in fits and algorithm.startswith(("context:","legacy:")):
                size = len(FEATURES) if algorithm.startswith("context:") else len(BASE_FEATURES)
                fits[algorithm] = _fit_moments(_moments(examples,size),float(algorithm.split(":")[1]))
        models[sku] = {"algorithm":chosen,"origin":series[0]["date"],"trained_through":series[-1]["date"],
                       "fit":fits.get(chosen),"without_context_algorithm":legacy,"without_context_fit":fits[legacy],
                       "reference_temperature_c":statistics.mean(env[r["date"]]["temperature_c"] for r in series if not r["stockout"] and not r["closed"]),
                       "reference_precipitation_mm":statistics.mean(env[r["date"]]["precipitation_mm"] for r in series if not r["stockout"] and not r["closed"])}
        report[sku] = {"selected_algorithm":chosen,"best_context_algorithm":contextual,
                       "best_without_context_algorithm":best_without,"best_no_environment_ridge_algorithm":legacy,
                       "best_simple_baseline_algorithm":baseline,
                       "selection_rule":"validation MAE; added complexity must improve at least1%; final56days never used for selection",
                       "validation_start":validation_start.isoformat(),"holdout_start":holdout_start.isoformat(),"holdout_end":end.isoformat(),
                       "validation_metrics":scores,"holdout_metrics":held_scores,
                       "validation_predictions":val_predictions,"holdout_predictions":held_predictions,
                       "holdout_holiday_metrics":_slice_metrics(held_predictions,finalists,True),
                       "holdout_nonholiday_metrics":_slice_metrics(held_predictions,finalists,False),
                       "training_holiday_days":sum(env[r["date"]]["is_holiday"] for r in series if _day(r["date"])<validation_start),
                       "records":len(series),"excluded_stockout_labels":sum(r["stockout"] for r in series),
                       "excluded_closed_labels":sum(r["closed"] for r in series)}
    artifact = {"version":VERSION,"data_source":"synthetic","created_at_utc":datetime.now(timezone.utc).isoformat(),
                "features":FEATURES,"training_data_sha256":_digest(rows),"environment_sha256":_digest(env),
                "models":models,"evaluation":report,
                "notes":["模拟销量与模拟日提前预报训练；没有真实门店预测准确率或大模型微调主张。",
                         "历史天气known_at不晚于目标前一天22:00；事后实况不能代替当时预报。",
                         "留出前28日验证选算法与正则化，最后56日只评估；逐日单步扩展前缀。",
                         "影响为同一模型的条件情景差，用两因素Shapley分摊天气/日历交互；非因果估计。",
                         "天气参考为本SKU训练可用日的平均预报气温/降水；假日参考为普通营业日。",
                         "KL日历中无中国式统一调休工作日；训练无此变化时不声称学会调休影响。"]}
    artifact["artifact_sha256"] = _digest(artifact)
    if output_path is not None:
        save_json(output_path,artifact)
    return artifact


def predict(model, sales, target_context):
    if not isinstance(model,dict) or model.get("version")!=VERSION or model.get("data_source")!="synthetic":
        raise CafeError("天气日历模型版本不兼容，请重新训练。")
    if model.get("artifact_sha256")!=_digest({k:v for k,v in model.items() if k!="artifact_sha256"}):
        raise CafeError("天气日历模型完整性校验失败。")
    context = validate_context(target_context)
    target = _day(context["date"])
    rows = validate_sales(sales)
    if set(model["models"])!={r["sku"] for r in rows}:
        raise CafeError("模型SKU与销量SKU不一致。")
    if any(_day(r["date"])>=target for r in rows):
        raise CafeError("输入含目标日或未来销量，存在泄漏。")
    forecasts, impacts, warnings = [], [], []
    cf = {name:[] for name in ("reference","weather_only","holiday_only")}
    for sku,spec in model["models"].items():
        series = [r for r in rows if r["sku"]==sku]
        if _day(series[-1]["date"])!=target-timedelta(days=1) or _day(spec["trained_through"])>=target:
            raise CafeError("销量必须截至预测前一天，模型不能训练到目标日。")
        if sum(not r["stockout"] and not r["closed"] for r in series)<28:
            raise CafeError(f"{sku}至少需要28条有效预测历史，不能只用最后一天代替完整输入。")
        origin = _day(spec["origin"])
        reference = dict(context,temperature_c=spec["reference_temperature_c"],precipitation_mm=spec["reference_precipitation_mm"],is_holiday=False,is_workday_override=False)
        weather_only = dict(reference,temperature_c=context["temperature_c"],precipitation_mm=context["precipitation_mm"])
        holiday_only = dict(reference,is_holiday=context["is_holiday"],is_workday_override=context["is_workday_override"])
        algorithm = spec["algorithm"]
        def estimate(c):
            if algorithm.startswith(("context:","legacy:")):
                return _estimate(spec["fit"],_features(series,target,origin,c,algorithm.startswith("context:")))
            return _baseline(series,target,algorithm)
        ref, weather, holiday, combined = (estimate(c) for c in (reference,weather_only,holiday_only,context))
        weather_delta = .5*((weather-ref)+(combined-holiday))
        holiday_delta = .5*((holiday-ref)+(combined-weather))
        # Round the displayed decomposition with an explicit residual so sums remain exact.
        ref6, combined6 = round(ref,6), round(combined,6)
        weather6 = round(weather_delta,6)
        holiday6 = round(combined6-ref6-weather6,6)
        forecasts.append({"sku":sku,"forecast_units":combined6,"algorithm":algorithm,"history_through":series[-1]["date"],
                          "holdout_mae_units":model["evaluation"][sku]["holdout_metrics"][algorithm]["mae_units"]})
        for name,val in (("reference",ref),("weather_only",weather),("holiday_only",holiday)):
            cf[name].append({"sku":sku,"forecast_units":round(val,6)})
        without = _estimate(spec["without_context_fit"],_features(series,target,origin,context,False))
        impacts.append({"sku":sku,"baseline_units":ref6,"weather_delta_units":weather6,
                        "holiday_delta_units":holiday6,"combined_units":combined6,
                        "without_context_model_units":round(without,6),
                        "reference_temperature_c":round(spec["reference_temperature_c"],4),
                        "reference_precipitation_mm":round(spec["reference_precipitation_mm"],4),
                        "context_used":algorithm.startswith("context:")})
        if not algorithm.startswith("context:"):
            warnings.append(f"{sku}天气日历模型未通过验证优势检查，本次使用{algorithm}，天气和假日影响为0。")
        if series[-1]["stockout"]:
            warnings.append(f"{sku}最近一天售罄，已排除其需求标签和滞后值，请人工核对。")
    if context["is_workday_override"]:
        warnings.append("调休工作日变化未在该KL模拟训练集中充分出现，此情景的调休影响尚未验证。")
    return {"target_date":context["date"],"items":forecasts,"warnings":warnings,"data_source":"synthetic",
            "model_version":VERSION,"model_sha256":model["artifact_sha256"],"context":context,
            "impact":{"items":impacts,"method":"two_factor_shapley","reference":"训练平均预报天气 + 普通营业日，保持相同历史销量",
                      "note":"条件情景差，仅反映此模拟数据模型的变化，不是现实因果影响或经营保证。"},
            "counterfactuals":{name:{"target_date":context["date"],"items":items,"warnings":[],"data_source":"synthetic"} for name,items in cf.items()},
            "note":"销量与训练天气均为模拟数据。真实天气只作为当前情景输入，不证明真实门店效果。"}


def make_demo_data(folder, end_date="2026-10-22", days=None):
    """Transparent scenario generator with latent weather and noisy day-ahead forecasts."""
    from cafe_context import holiday_for_day
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    end=_day(end_date)
    start=date(end.year,1,1) if days is None else end-timedelta(days=days-1)
    if start.year!=2026 or end.year!=2026 or (end-start).days+1<MIN_ROWS:
        raise CafeError("演示生成器使用已核对的2026吉隆坡日历，至少112天。")
    rng=random.Random(20261023032)
    sales,environment,skipped_dates=[],[],[]
    weekly=(.92,.96,.99,1.03,1.12,1.32,1.20)
    day=start
    i=0
    while day<=end:
        holiday=holiday_for_day(day.isoformat())
        if holiday.get("is_holiday") is None or holiday.get("synthetic_backtest_policy")=="skip_target":
            skipped_dates.append({"date":day.isoformat(),"reason":"假日适用情况或公告可知时刻不确定；不生成该日销量或训练天气。"})
            day+=timedelta(days=1)
            i+=1
            continue
        temp=max(25.0,min(34.0,29.0+2.3*math.sin(i*.37)+rng.gauss(0,.65)))
        rain=0.0 if rng.random()<.43 else min(38.0,rng.expovariate(1/9))
        # These forecast errors are independent noise, not labels fed back to features.
        realized_temp=temp+rng.gauss(0,.7)
        realized_rain=max(0.0,rain+rng.gauss(0,2.5))
        weather_known=datetime.combine(day-timedelta(days=1),time(18),KL)
        calendar_known=holiday.get("calendar_known_from")
        combined_known=max(weather_known,_instant(calendar_known,"calendar_known_from")) if calendar_known else weather_known
        env={"date":day.isoformat(),"temperature_c":round(temp,3),"precipitation_mm":round(rain,3),
             "is_holiday":holiday["is_holiday"],"is_workday_override":bool(holiday.get("is_workday_override",False)),
             "known_at":combined_known.isoformat(),"weather_known_at":weather_known.isoformat(),
             "weather_kind":"synthetic_forecast","location":"Kuala Lumpur, Malaysia",
             "source":"公开模拟天气生成器；不是历史气象实况或历史API预报",
             "holiday_name":holiday.get("holiday_name",""),"calendar_source":holiday.get("source","")}
        if calendar_known:
            env["calendar_known_from"]=calendar_known
        environment.append(env)
        for sku,base,slope,thermal,wet,holiday_boost in (("espresso",58,.035,.8,-.70,14),
             ("latte",76,.045,1.3,-.95,20),("oat_latte",34,.065,1.6,-.60,12)):
            boost=holiday_boost if env["is_holiday"] else 0
            # Synthetic scenario only: it does not assert any universal weather multiplier.
            demand=(base+slope*i)*weekly[day.weekday()] + thermal*(realized_temp-29) + wet*realized_rain + boost
            if env["is_holiday"]:
                demand += -.15*realized_rain
            demand=max(0,round(demand+rng.gauss(0,3)))
            stockout=i%67==41 and sku=="oat_latte"
            units=round(demand*.65) if stockout else demand
            sales.append({"date":day.isoformat(),"sku":sku,"units":units,"stockout":stockout,"closed":False,"data_source":"synthetic"})
        i+=1
        day+=timedelta(days=1)
    with (folder/"sales.csv").open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=["date","sku","units","stockout","closed","data_source"])
        writer.writeheader();writer.writerows(sales)
    save_json(folder/"environment.json",environment)
    save_json(folder/"data_manifest.json",{"data_source":"synthetic","seed":20261023032,"days":len(environment),
        "sales_rows":len(sales),"start_date":start.isoformat(),"end_date":end.isoformat(),
        "contains_real_business_records":False,"weather_source":"synthetic day-ahead forecasts with forecast errors",
        "forecast_issue_time":"weather at target-1 18:00 +08:00; combined known_at also respects later calendar_known_from",
        "backtest_origin":"target-1 22:00 +08:00",
        "generator":"cafe_forecast_context.make_demo_data","skipped_dates":skipped_dates,
        "formula":"(base+slope*day_index)*weekday_factor + temp_coefficient*(latent_temperature-29) + rain_coefficient*latent_rain + holiday_boost - 0.15*latent_rain*holiday + Gaussian(0,3)",
        "forecast_error":"latent_temperature = forecast_temperature + Gaussian(0,0.7); latent_rain = max(0, forecast_rain + Gaussian(0,2.5)); latent weather never passed to model features",
        "sku_parameters":{"espresso":{"base":58,"slope":0.035,"temperature_coefficient":0.8,"rain_coefficient":-0.70,"holiday_boost":14},
                          "latte":{"base":76,"slope":0.045,"temperature_coefficient":1.3,"rain_coefficient":-0.95,"holiday_boost":20},
                          "oat_latte":{"base":34,"slope":0.065,"temperature_coefficient":1.6,"rain_coefficient":-0.60,"holiday_boost":12}},
        "weekday_factors":weekly,"note":"天气—销量关系仅是公开模拟场景，不是对真实咖啡店的经验结论。"})
    return sales,environment


def make_demo_context_data(directory, end_date="2026-10-22"):
    """Write the fully synthetic fixture and an explicitly simulated tomorrow scenario."""
    from cafe_context import holiday_for_day
    sales,environment=make_demo_data(directory,end_date=end_date)
    target=_day(end_date)+timedelta(days=1)
    holiday=holiday_for_day(target.isoformat())
    if type(holiday.get("is_holiday")) is not bool:
        raise CafeError("明日假日适用情况不确定，请先核对后再生成情景。")
    context={"date":target.isoformat(),"temperature_c":28.4,"precipitation_mm":12.0,
             "is_holiday":holiday["is_holiday"],"is_workday_override":False,
             "known_at":datetime.combine(target-timedelta(days=1),time.min,KL).isoformat(),
             "weather_kind":"synthetic_forecast","location":"Kuala Lumpur, Malaysia",
             "source":"模拟明日预报；用于演示，未调用真实天气服务",
             "holiday_name":holiday.get("holiday_name",""),"calendar_source":holiday.get("source","")}
    context=validate_context(context)
    save_json(Path(directory)/"tomorrow_context.json",context)
    save_json(Path(directory)/"target_context.json",context)
    return sales,environment,context


def save_training_report(model, path):
    save_json(path,{k:model[k] for k in ("version","training_data_sha256","environment_sha256","artifact_sha256","features","evaluation","notes")})
    def wape_text(metric):
        return f"{metric['wape']*100:.2f}%" if metric['wape'] is not None else "不估计"
    lines=["# 吉隆坡咖啡补货：天气和节假日模型训练报告", "", 
           "本报告只评估模拟销量。历史天气是带预报误差的模拟事前快照，不是下载的历史实况，也不是从气象商取得的历史预报。真实天气输入不代表已经验证真实门店效果。", "",
           "## 时间与选择方法", "",
           "末尾 56 个日历日作为独立留出窗口，之前 28 日作为验证窗口。所有算法、正则化强度只按验证 MAE 选择；复杂模型须比备选至少改善 1%。留出结果不参与选择。每一天滚动扩展历史前缀，预测起点为目标日前一天吉隆坡时间 22:00，允许使用之前已出现的留出日销量，不能看到当日销量。", "",
           "比较昨日可用销量、近四个同星期均值、无环境特征的 Ridge、含温度/降雨/假日/调休及天气×假日交互的 Ridge。Ridge 候选 α 为 0.1、1、10。售罄与休业日既不作标签，也不进入需求滞后值。", "",
           "## 留出评估", "", "| SKU | 验证选择 | 算法 | MAE（杯） | WAPE | 样本数 |", "|---|---|---|---:|---:|---:|"]
    for sku,r in model["evaluation"].items():
        for algorithm,metric in r["holdout_metrics"].items():
            lines.append(f"| {sku} | {'✓' if algorithm==r['selected_algorithm'] else ''} | {algorithm} | {metric['mae_units']:.3f} | {wape_text(metric)} | {metric['n']} |")
    lines += ["", "## 假日与普通日分组", "", "| SKU | 分组 | 已选择算法 MAE | WAPE | 样本数 |", "|---|---|---:|---:|---:|"]
    for sku,r in model["evaluation"].items():
        for label,key in (("假日","holdout_holiday_metrics"),("非假日","holdout_nonholiday_metrics")):
            group=r[key]
            if group.get("n")==0:
                lines.append(f"| {sku} | {label} | 不估计 | 不估计 | 0 |")
            else:
                metric=group[r["selected_algorithm"]]
                lines.append(f"| {sku} | {label} | {metric['mae_units']:.3f} | {wape_text(metric)} | {metric['n']} |")
    first=min(r['holdout_start'] for r in model['evaluation'].values())
    last=max(r['holdout_end'] for r in model['evaluation'].values())
    lines += ["", f"假日样本很少，不能据此宣称对所有节日准确。本次留出窗口 {first} 至 {last}；无环境模型及各组完整对照见 JSON 报告。", "",
              "## 可复现与解释", "",
              "生成器在 cafe_forecast_context.make_demo_data，固定随机种子、逐 SKU 公式、所有参数和跳过日期见 examples_context/data_manifest.json。2026-02-03 的私人雇主假日适用条件不确定，2026-03-20 额外假日公告只核对到日期而无精确时刻，因此训练中完全跳过这两日，不擅自回填为当时已知。当前日历仍正确显示额外公共假日。", "",
              "未来影响采用同一已拟合模型，保持相同销售历史，分别计算训练平均预报天气+普通营业日、实际天气、实际日历、两者同时输入。两因素 Shapley 平均两种加入顺序，天气与假日差额准确相加等于总变化。训练平均降水是参考，不默认零降水。该差额是模型条件情景解释，未经验证为现实因果效应。", "",
              "KL 数据没有统一调休工作日变化，相关特征虽进入模型但本次未学得可验证的调休效应；不能外推为中国式调休日历。", "",
              f"销售 SHA256：`{model['training_data_sha256']}`", "",
              f"环境 SHA256：`{model['environment_sha256']}`", "",
              f"模型 SHA256：`{model['artifact_sha256']}`", "",
              "模型 JSON 持久化特征顺序、标准化均值/尺度和真实拟合权重；报告 JSON 包含逐日目标、前缀截止日、天气可知时间、预测起点、原始预测与误差，便于复查。", ""]
    Path(path).with_suffix(".md").write_text("\n".join(lines),encoding="utf-8")


if __name__=="__main__":
    base=Path(__file__).resolve().parent
    sales,env,context=make_demo_context_data(base/"examples_context")
    model=train(sales,env,base/"models"/"demand_context_v32.json")
    save_training_report(model,base/"reports"/"training_context_v32.json")
    print(json.dumps({sku:{"selected":r["selected_algorithm"],"holdout":r["holdout_metrics"]} for sku,r in model["evaluation"].items()},ensure_ascii=False,indent=2))
