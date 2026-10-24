"""Context boundaries: an absent input must never become dry weather/ordinary day."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cafe_context import (ContextUnavailable, KL, _parse_open_meteo, _weather_url,
                          fetch_weather, holiday_for_day, validate_weather_snapshot)


NOW = datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc)
TARGET = "2026-10-23"


def payload():
    return {"latitude": 3.125, "longitude": 101.6875,
            "timezone": "Asia/Kuala_Lumpur", "utc_offset_seconds": 28800,
            "daily_units": {"time": "iso8601", "temperature_2m_mean": "°C", "precipitation_sum": "mm"},
            "daily": {"time": [TARGET], "temperature_2m_mean": [28.2], "precipitation_sum": [12.4]}}


def snapshot():
    from datetime import date
    day = date.fromisoformat(TARGET)
    return _parse_open_meteo(payload(), day, NOW, _weather_url(day))


class Response(io.BytesIO):
    def getcode(self):
        return 200


class WeatherContextTests(unittest.TestCase):
    def test_fetch_exact_local_target_and_persist_attributed_snapshot(self):
        base = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(dir=base) as directory:
            output = Path(directory) / "weather.json"
            with patch("cafe_context._utc_now", return_value=NOW), patch("cafe_context.urllib.request.urlopen", return_value=Response(json.dumps(payload()).encode())) as request:
                actual = fetch_weather(TARGET, output)
            called = request.call_args.args[0].full_url
            self.assertTrue(called.startswith("https://api.open-meteo.com/v1/forecast?"))
            self.assertIn("timezone=Asia%2FKuala_Lumpur", called)
            self.assertIn("start_date=2026-10-23", called)
            self.assertEqual(actual["temperature_c"], 28.2)
            self.assertEqual(actual["precipitation_mm"], 12.4)
            self.assertEqual(actual["known_at"], NOW.isoformat())
            self.assertEqual(actual["weather_kind"], "live_forecast")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), actual)

    def test_missing_null_nonfinite_and_negative_rain_never_become_zero(self):
        from datetime import date
        for value in (None, "0", True, float("nan"), -1):
            data = payload()
            data["daily"]["precipitation_sum"] = [value]
            with self.subTest(value=value), self.assertRaises(ContextUnavailable):
                _parse_open_meteo(data, date.fromisoformat(TARGET), NOW, _weather_url(date.fromisoformat(TARGET)))
        valid_dry = payload()
        valid_dry["daily"]["precipitation_sum"] = [0]
        self.assertEqual(_parse_open_meteo(valid_dry, date.fromisoformat(TARGET), NOW, _weather_url(date.fromisoformat(TARGET)))["precipitation_mm"], 0)

    def test_provider_date_units_timezone_and_location_are_checked(self):
        from datetime import date
        modifications = [lambda x: x["daily"].update(time=["2026-10-24"]),
                         lambda x: x["daily"].update(time=[TARGET, TARGET]),
                         lambda x: x["daily_units"].update(precipitation_sum="inch"),
                         lambda x: x.update(timezone="UTC", utc_offset_seconds=0),
                         lambda x: x.update(latitude=1.29, longitude=103.85),
                         lambda x: x.update(daily=None),
                         lambda x: x.update(error=True)]
        for alter in modifications:
            data = payload()
            alter(data)
            with self.subTest(modification=alter), self.assertRaises(ContextUnavailable):
                _parse_open_meteo(data, date.fromisoformat(TARGET), NOW, _weather_url(date.fromisoformat(TARGET)))

    def test_stale_future_timestamp_and_wrong_cached_date_are_rejected(self):
        actual = snapshot()
        stale = copy.deepcopy(actual)
        stale["fetched_at"] = stale["known_at"] = (NOW - timedelta(hours=25)).isoformat()
        future = copy.deepcopy(actual)
        future["fetched_at"] = future["known_at"] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaises(ContextUnavailable):
            validate_weather_snapshot(stale, TARGET, NOW)
        with self.assertRaises(ContextUnavailable):
            validate_weather_snapshot(future, TARGET, NOW)
        with self.assertRaises(ContextUnavailable):
            validate_weather_snapshot(actual, "2026-10-24", NOW)
        altered_source = copy.deepcopy(actual)
        altered_source["source"] = actual["source"].replace("start_date=2026-10-23", "start_date=2026-10-24")
        with self.assertRaises(ContextUnavailable):
            validate_weather_snapshot(altered_source, TARGET, NOW)

    def test_synthetic_or_observation_cannot_pass_live_validator(self):
        for kind in ("synthetic_forecast", "historical_observation", "live_forecast"):
            actual = snapshot()
            actual["data_kind"] = actual["weather_kind"] = kind
            if kind == "live_forecast":
                actual["known_at"] = (NOW - timedelta(days=7)).isoformat()
            with self.subTest(kind=kind), self.assertRaises(ContextUnavailable):
                validate_weather_snapshot(actual, TARGET, NOW)

    def test_historical_targets_are_rejected_before_network_request(self):
        with patch("cafe_context._utc_now", return_value=NOW), patch("cafe_context.urllib.request.urlopen") as request:
            for target in ("2026-10-21", "2026-10-22", "2026-11-07", "2026-02-30"):
                with self.subTest(target=target), self.assertRaises(ContextUnavailable):
                    fetch_weather(target)
            request.assert_not_called()

    def test_network_failure_preserves_existing_snapshot(self):
        base = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(dir=base) as directory:
            output = Path(directory) / "weather.json"
            output.write_text("prior snapshot", encoding="utf-8")
            with patch("cafe_context._utc_now", return_value=NOW), patch("cafe_context.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
                with self.assertRaisesRegex(ContextUnavailable, "0 降雨"):
                    fetch_weather(TARGET, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "prior snapshot")

    def test_utc_date_rollover_uses_kl_date(self):
        # UTC Oct22 18:00 is KL Oct23 02:00; Oct23 is no longer tomorrow.
        with self.assertRaises(ContextUnavailable):
            validate_weather_snapshot(snapshot(), TARGET, datetime(2026, 10, 22, 18, tzinfo=timezone.utc))
        self.assertEqual(NOW.astimezone(KL).hour, 21)


class HolidayContextTests(unittest.TestCase):
    def test_kl_not_selangor_or_other_state_calendar(self):
        for day in ("2026-12-11", "2026-05-28", "2026-03-23"):
            result = holiday_for_day(day)
            self.assertEqual(result["location"], "Kuala Lumpur")
        self.assertFalse(holiday_for_day("2026-12-11")["is_holiday"])
        self.assertFalse(holiday_for_day("2026-05-28")["is_holiday"])
        self.assertNotIn("柔佛", holiday_for_day("2026-03-23")["holiday_name"])

    def test_double_holiday_private_employer_condition_stays_unknown(self):
        self.assertTrue(holiday_for_day("2026-02-01")["is_holiday"])
        self.assertTrue(holiday_for_day("2026-02-02")["is_holiday"])
        conditional = holiday_for_day("2026-02-03")
        self.assertIsNone(conditional["is_holiday"])
        self.assertFalse(conditional["verified"])
        self.assertIn("JTK_", conditional["source"])
        self.assertEqual(conditional["status"], "conditional_employer_selection")

    def test_extra_raya_and_observed_collision_are_attributed(self):
        self.assertIn("PUB-111", holiday_for_day("2026-03-20")["source"])
        self.assertEqual(holiday_for_day("2026-03-20")["synthetic_backtest_policy"], "skip_target")
        self.assertNotIn("calendar_known_from", holiday_for_day("2026-03-20"))
        for target in ("2026-03-20", "2026-03-21", "2026-03-22", "2026-03-23", "2026-06-01", "2026-06-02", "2026-11-09"):
            row = holiday_for_day(target)
            self.assertTrue(row["is_holiday"])
            self.assertTrue(row["verified"])
            self.assertTrue(row["source"].startswith("https://"))
        self.assertIn("卫塞", holiday_for_day("2026-06-02")["holiday_name"])
        self.assertIn("subject_to_change", holiday_for_day("2026-11-09")["status"])

    def test_unknown_year_and_weekend_are_distinct(self):
        for target in ("2025-12-25", "2027-01-01"):
            row = holiday_for_day(target)
            self.assertIsNone(row["is_holiday"])
            self.assertFalse(row["verified"])
            self.assertEqual(row["status"], "unknown")
        saturday = holiday_for_day("2026-10-24")
        self.assertIs(saturday["is_holiday"], False)
        self.assertIs(saturday["is_workday_override"], False)


if __name__ == "__main__":
    unittest.main()
