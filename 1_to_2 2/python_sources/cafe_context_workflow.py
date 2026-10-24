"""Optional KL weather/calendar workflow, with explicit provenance and immutable evidence."""
from copy import deepcopy
from datetime import datetime, timedelta

from cafe_core import CafeError, save_json
from cafe_service import SHANGHAI, load_json


def valid_context(service, context, target):
    from cafe_forecast_context import validate_context
    from cafe_context import validate_weather_snapshot, holiday_for_day
    value = validate_context(context, target)
    if value["weather_kind"] == "manual_scenario":
        raise CafeError("假设情景仅供比较，不能作为采购审批输入。")
    instant = service.clock().astimezone(SHANGHAI)
    if datetime.fromisoformat(value["known_at"]) > instant:
        raise CafeError("天气快照的获取时间尚未到达，不能用于当前审批。")
    if instant - datetime.fromisoformat(value["known_at"]) > timedelta(hours=48):
        raise CafeError("天气快照已过期，请刷新天气并重新生成建议。")
    if value["weather_kind"] == "live_forecast":
        validate_weather_snapshot(value, target, now=instant)
    if value["weather_kind"] != "manual_scenario":
        holiday = holiday_for_day(target)
        if holiday["is_holiday"] is None or not holiday.get("verified", False):
            raise CafeError("该日期的吉隆坡假日状态尚未确认，不能当普通工作日审批。")
        if value["is_holiday"] != holiday["is_holiday"]:
            raise CafeError("当前假日标记与吉隆坡已核验日历不符，请刷新天气日历。")
    return value


def analysis(inputs, forecast, actual_plan):
    from cafe_materials import plan
    comparisons = {}
    for key, alternative in forecast.get("counterfactuals", {}).items():
        comparisons[key] = plan(alternative, inputs["inventory"], inputs["recipes"], inputs["suppliers"], forecast["target_date"])
    reference = {x["ingredient_id"]: x for x in comparisons.get("reference", {}).get("items", [])}
    items = []
    for item in actual_plan["items"]:
        ref = reference.get(item["ingredient_id"], item)
        items.append({k: item[k] for k in ("ingredient_id", "name", "unit", "pack_size", "purchase_unit")} | {
            "reference_quantity": ref["suggested_quantity"], "actual_quantity": item["suggested_quantity"],
            "delta_quantity": item["suggested_quantity"] - ref["suggested_quantity"]})
    return {"context": deepcopy(inputs["target_context"]), "impact": forecast.get("impact", {}),
            "ingredients": items, "reference_description": "同一模型、同一销售历史与库存；参考情景为训练期平均天气、非假日。差值是模型情景比较，不能视为真实经营中的因果效果。"}


def context_state(service, inputs):
    if "target_context" not in inputs:
        return {"enabled": False}
    target = (service.clock().astimezone(SHANGHAI).date() + timedelta(days=1)).isoformat()
    error = None
    try:
        valid_context(service, inputs["target_context"], target)
    except (CafeError, ValueError, KeyError) as exc:
        error = str(exc)
    latest = next((p for p in service.store.snapshot()["proposals"] if p["target_date"] == target), None)
    status_path = service.runtime / "context_fetch_status.json"
    return {"enabled": True, "status": "needs_refresh" if error else "ready", "error": error,
            "snapshot": inputs["target_context"], "analysis": latest.get("analysis") if latest else None,
            "analysis_current": bool(not error and latest and service.model_path.exists() and latest["input_hash"] == service.source_hash(inputs)),
            "last_fetch": load_json(status_path) if status_path.exists() else None}


def refresh_weather(service):
    from cafe_context import fetch_weather, holiday_for_day
    instant = service.clock().astimezone(SHANGHAI)
    target = (instant.date() + timedelta(days=1)).isoformat()
    inputs = service.inputs()
    if "environment" not in inputs:
        raise CafeError("请先运行 demo-context 准备天气训练资料。")
    try:
        weather = fetch_weather(target)
        holiday = holiday_for_day(target)
        if holiday["is_holiday"] is None or not holiday.get("verified", False):
            raise CafeError("该日吉隆坡假日尚待确认；保留上次天气，不发布新建议。")
        context = dict(weather, weather_kind="live_forecast", forecast_origin=instant.isoformat(),
                       is_holiday=holiday["is_holiday"], is_workday_override=holiday.get("is_workday_override", False),
                       holiday_name=holiday.get("holiday_name", ""), holiday_source=holiday.get("source", ""),
                       holiday_verified=holiday.get("verified", False), holiday_notes=holiday.get("notes", []),
                       holiday_sources=holiday.get("sources", []), holiday_status=holiday.get("status"))
        # fetched_at can be milliseconds later than the method's initial clock read.
        context["forecast_origin"] = max(instant, datetime.fromisoformat(context["known_at"].replace("Z", "+00:00"))).isoformat()
        valid_context(service, context, target)
        save_json(service.data_dir / "target_context.json", context)
        status = {"ok": True, "at": service.clock().isoformat(), "source": context.get("source")}
        save_json(service.runtime / "context_fetch_status.json", status)
        service.store.log("weather_refreshed", {"date": target, "context": context})
        return {"context": context, "message": "天气日历已刷新，请重新生成建议后审批。"}
    except (CafeError, OSError, ValueError, KeyError) as exc:
        save_json(service.runtime / "context_fetch_status.json", {"ok": False, "at": service.clock().isoformat(), "error": str(exc)})
        raise CafeError("天气更新未完成，已保留上次快照：" + str(exc)) from None


def scenario(service, values):
    from cafe_forecast_context import predict, validate_context
    from cafe_materials import plan
    inputs = service.inputs()
    if "target_context" not in inputs or not service.model_path.exists():
        raise CafeError("先准备天气资料并训练模型，再比较假设情景。")
    stamp = service.runtime / "training_source.json"
    if not stamp.exists() or load_json(stamp) != service.training_stamp(inputs):
        raise CafeError("训练资料已更新，请先重新生成建议，再比较情景。")
    context = dict(inputs["target_context"])
    for key in ("temperature_c", "precipitation_mm", "is_holiday"):
        if key not in values:
            raise CafeError("请填写温度、降水和假日假设。")
        context[key] = values[key]
    context.pop("fetched_at", None)
    context.update(weather_kind="manual_scenario", data_kind="synthetic_override", provider="manual_scenario",
                   holiday_verified=False, holiday_source="用户假设", holiday_sources=[], known_at=service.clock().isoformat(),
                   forecast_origin=service.clock().isoformat(), holiday_name="用户假设的公共假日" if context["is_holiday"] else "假设非假日",
                   source="手机工作台假设情景", scenario=True)
    context = validate_context(context, inputs["target_context"]["date"])
    model = load_json(service.model_path)
    forecast = predict(model, inputs["sales"], context)
    hypothetical_inputs = dict(inputs, target_context=context)
    procurement = plan(forecast, inputs["inventory"], inputs["recipes"], inputs["suppliers"], context["date"])
    actual = predict(model, inputs["sales"], inputs["target_context"])
    actual_plan = plan(actual, inputs["inventory"], inputs["recipes"], inputs["suppliers"], context["date"])
    return {"scenario": True, "persisted": False, "forecast": forecast, "plan": procurement,
            "analysis": analysis(hypothetical_inputs, forecast, procurement), "actual_plan": actual_plan,
            "actual_forecast": actual, "note": "假设情景，不改变真实假日日历、当前建议、库存或采购草稿。"}
