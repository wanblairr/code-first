"""Kuala Lumpur's attributed holiday calendar and strictly future weather forecasts.

Live weather is an input snapshot, never a historical training observation. This
module performs no network request at import time or when reading the calendar.
The competition's generated weather must be labelled ``synthetic_forecast`` by
its generator; it cannot pass the live-snapshot validator below.
"""
from __future__ import annotations

import argparse
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cafe_core import CafeError


KL = timezone(timedelta(hours=8), "Asia/Kuala_Lumpur")
LOCATION = "Kuala Lumpur"
TIMEZONE = "Asia/Kuala_Lumpur"
LATITUDE, LONGITUDE = 3.139, 101.6869
WEATHER_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
WEATHER_DOCUMENTATION = "https://open-meteo.com/en/docs"
CALENDAR_PATH = Path(__file__).resolve().parent / "data" / "calendar_kl_2026.json"
MAX_FORECAST_DAYS_AHEAD = 15
MAX_WEATHER_AGE_HOURS = 24


class ContextUnavailable(CafeError):
    """A missing, stale or inconsistent context must be shown to the operator."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day(value: str | date) -> date:
    if isinstance(value, datetime):
        raise ContextUnavailable("日期必须为 YYYY-MM-DD，不能用时间戳代替门店日期。")
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContextUnavailable("日期必须为有效的 YYYY-MM-DD。") from exc
    if value != parsed.isoformat():
        raise ContextUnavailable("日期必须为 YYYY-MM-DD。")
    return parsed


def _instant(value: str | datetime, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ContextUnavailable(f"{label}必须为含时区的 ISO 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextUnavailable(f"{label}缺少时区。")
    return parsed


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContextUnavailable(f"{label}缺失或不是数值，不能按 0 处理。")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ContextUnavailable(f"{label}超出有效范围。")
    return result


def _check_future_target(target: date, now: datetime) -> None:
    ahead = (target - now.astimezone(KL).date()).days
    if not 1 <= ahead <= MAX_FORECAST_DAYS_AHEAD:
        raise ContextUnavailable("实时天气仅支持吉隆坡明天至未来 15 天；历史实况不能冒充事前预报。")


def _weather_url(target: date) -> str:
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "temperature_2m_mean,precipitation_sum",
        "timezone": TIMEZONE,
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
    }
    return WEATHER_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _parse_open_meteo(payload: dict, target: date, fetched_at: datetime, source: str) -> dict:
    """Match the requested local day, units and location before accepting a row."""
    if not isinstance(payload, dict) or payload.get("error"):
        raise ContextUnavailable("天气服务返回错误，暂无可用天气预报。")
    if payload.get("timezone") != TIMEZONE or payload.get("utc_offset_seconds") != 28800:
        raise ContextUnavailable("天气服务的日期时区不是吉隆坡 UTC+8。")
    actual_lat = _number(payload.get("latitude"), "天气纬度", -90, 90)
    actual_lon = _number(payload.get("longitude"), "天气经度", -180, 180)
    # The forecast grid cell need not equal the requested coordinate exactly.
    if abs(actual_lat - LATITUDE) > .5 or abs(actual_lon - LONGITUDE) > .5:
        raise ContextUnavailable("天气服务返回的地点不在吉隆坡附近。")
    daily, units = payload.get("daily"), payload.get("daily_units")
    if not isinstance(daily, dict) or not isinstance(units, dict):
        raise ContextUnavailable("天气服务缺少逐日数据或单位。")
    if units.get("temperature_2m_mean") != "°C" or units.get("precipitation_sum") != "mm":
        raise ContextUnavailable("天气单位不符，需摄氏度和毫米。")
    days, temperatures, rain = (daily.get("time"), daily.get("temperature_2m_mean"), daily.get("precipitation_sum"))
    if not all(isinstance(part, list) for part in (days, temperatures, rain)):
        raise ContextUnavailable("天气服务缺少日期、日均温或日降水量。")
    if len(days) != len(temperatures) or len(days) != len(rain):
        raise ContextUnavailable("天气服务的日期与数值数量不一致。")
    target_text = target.isoformat()
    if days.count(target_text) != 1:
        raise ContextUnavailable("天气服务未返回目标日期，不能沿用其他日期的天气。")
    index = days.index(target_text)
    result = {
        "date": target_text,
        "temperature_c": _number(temperatures[index], "日均温", -90, 65),
        "precipitation_mm": _number(rain[index], "日降水量", 0, 3000),
        "temperature_unit": "°C",
        "precipitation_unit": "mm",
        "data_kind": "live_forecast",
        "weather_kind": "live_forecast",
        "source": source,
        "provider": "Open-Meteo",
        "documentation": WEATHER_DOCUMENTATION,
        "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
        "fetched_at": fetched_at.isoformat(),
        "known_at": fetched_at.isoformat(),
        "location": LOCATION,
        "timezone": TIMEZONE,
        "utc_offset_seconds": 28800,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "forecast_grid_latitude": actual_lat,
        "forecast_grid_longitude": actual_lon,
        "status": "available",
        "max_age_hours": MAX_WEATHER_AGE_HOURS,
        "note": "当前取得的未来预报；不是历史天气或真实门店销量。",
    }
    return validate_weather_snapshot(result, target, now=fetched_at)


def validate_weather_snapshot(weather: dict, target_date: str | date,
                              now: str | datetime | None = None,
                              max_age_hours: float = MAX_WEATHER_AGE_HOURS) -> dict:
    """Return a checked copy of a *live* forecast; stale/missing data raises.

    Freshness is time since retrieval, not a claimed upstream model issue time.
    This is deliberately unsuitable for reconstructing historical backtests.
    """
    target = _day(target_date)
    instant = _instant(now or _utc_now(), "当前时间")
    _check_future_target(target, instant)
    if not isinstance(weather, dict) or weather.get("date") != target.isoformat():
        raise ContextUnavailable("天气快照的日期与本次补货目标不一致。")
    if weather.get("data_kind") != "live_forecast" or weather.get("weather_kind") != "live_forecast":
        raise ContextUnavailable("此输入不是实时未来预报；模拟天气必须单独标注。")
    if weather.get("location") != LOCATION or weather.get("timezone") != TIMEZONE:
        raise ContextUnavailable("天气快照的门店地点或时区不符。")
    if weather.get("utc_offset_seconds") != 28800:
        raise ContextUnavailable("天气快照必须使用吉隆坡 UTC+8 日界线。")
    if weather.get("temperature_unit") != "°C" or weather.get("precipitation_unit") != "mm":
        raise ContextUnavailable("天气快照缺少摄氏度或毫米单位。")
    fetched = _instant(weather.get("fetched_at"), "天气获取时间")
    known = _instant(weather.get("known_at"), "天气可知时间")
    if known != fetched:
        raise ContextUnavailable("实时快照的可知时间必须等于本次获取时间，不能回填成历史预报。")
    if fetched > instant:
        raise ContextUnavailable("天气获取时间晚于当前预测时间。")
    if not isinstance(max_age_hours, (int, float)) or isinstance(max_age_hours, bool) or not 0 < max_age_hours <= 24:
        raise ContextUnavailable("天气有效期必须在 0 至 24 小时内。")
    if instant - fetched > timedelta(hours=max_age_hours):
        raise ContextUnavailable("天气快照已过期，请手动刷新；过期数据不会当作当前预报使用。")
    _check_future_target(target, fetched)
    source = weather.get("source")
    parsed_source = urllib.parse.urlsplit(source) if isinstance(source, str) else None
    if (not parsed_source or parsed_source.scheme != "https" or parsed_source.netloc != "api.open-meteo.com"
            or parsed_source.path != "/v1/forecast" or weather.get("provider") != "Open-Meteo"):
        raise ContextUnavailable("天气快照没有固定的 Open-Meteo 预报来源。")
    if source != _weather_url(target):
        raise ContextUnavailable("天气快照的来源查询与吉隆坡目标日期不一致。")
    result = dict(weather)
    result["temperature_c"] = _number(weather.get("temperature_c"), "日均温", -90, 65)
    result["precipitation_mm"] = _number(weather.get("precipitation_mm"), "日降水量", 0, 3000)
    result["status"] = "available"
    result["age_hours"] = round((instant - fetched).total_seconds() / 3600, 3)
    return result


def fetch_weather(target_date: str | date, output_path: str | Path | None = None) -> dict:
    """Explicitly fetch KL forecast from one fixed public HTTPS endpoint.

    No API key, arbitrary URL, observed-weather endpoint or automatic fallback is
    supported. An optional output is replaced only after the entire response has
    passed validation. Failed refreshes preserve the prior file for inspection.
    """
    target = _day(target_date)
    _check_future_target(target, _utc_now())
    url = _weather_url(target)
    request = urllib.request.Request(url, headers={"User-Agent": "CafeReplenishmentDemo/3.2", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.getcode() != 200:
                raise ContextUnavailable("天气服务暂不可用，请稍后手动重试或选择有明确标识的模拟情景。")
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ContextUnavailable("天气服务响应过大。")
            payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextUnavailable("无法取得吉隆坡天气预报；缺失天气不会按晴天或 0 降雨处理。") from exc
    result = _parse_open_meteo(payload, target, _utc_now(), url)
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    return result


def holiday_for_day(target_date: str | date) -> dict:
    """2026 KL calendar only: a weekend is not itself a named public holiday.

    ``verified`` means checked against the linked official publication. It does
    not promise that future, changeable dates can never be amended. Conditional
    private-employer leave and years outside this finite calendar stay unknown.
    Malaysia has no Chinese-style make-up workday in this verified calendar.
    """
    target = _day(target_date)
    try:
        calendar = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextUnavailable("吉隆坡节假日日历文件不可用，请人工核对。") from exc
    base = {
        "date": target.isoformat(), "location": LOCATION, "region": "MY-14",
        "timezone": TIMEZONE, "is_holiday": None, "holiday_name": "",
        "is_workday_override": False, "source": calendar["sources"]["annual"]["url"],
        "verified": False, "verified_as_of": calendar["verified_as_of"],
        "coverage_start": calendar["coverage_start"], "coverage_end": calendar["coverage_end"],
        "status": "unknown", "notes": "仅收录 2026 年吉隆坡适用公共假日；未覆盖日期不能视作普通营业日。",
        "data_kind": "official_calendar",
    }
    if not calendar["coverage_start"] <= target.isoformat() <= calendar["coverage_end"]:
        return base
    entry = next((row for row in calendar["holidays"] if row["date"] == target.isoformat()), None)
    if entry is not None:
        base.update({key: value for key, value in entry.items() if key != "source_ids"})
        evidence = [calendar["sources"][key] for key in entry["source_ids"]]
        base["source"] = evidence[0]["url"]
        base["sources"] = evidence
        return base
    base.update({"is_holiday": False, "verified": True, "status": "not_listed_as_public_holiday",
                 "notes": "2026 吉隆坡已核对年历未列为公共假日；周末由星期特征处理，门店及供应商休息日需另设。",
                 "sources": [calendar["sources"]["annual"]]})
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description="吉隆坡天气与 2026 节假日查询")
    parser.add_argument("kind", choices=("weather", "holiday"))
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = fetch_weather(args.date, args.output) if args.kind == "weather" else holiday_for_day(args.date)
    except ContextUnavailable as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
