"""Context refresh, model caching and approval evidence across the full service."""
import copy
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cafe_app import prepare_demo
from cafe_context import ContextUnavailable, _parse_open_meteo, _weather_url
from cafe_core import CafeError, save_json
from cafe_forecast_context import make_demo_context_data, train
from cafe_service import CafeService, SHANGHAI, load_json


BASE = Path(__file__).resolve().parent.parent


class ServiceContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.gettempdir()).resolve()
        cls.fixture_temp = tempfile.TemporaryDirectory(dir=cls.scratch)
        cls.fixture = Path(cls.fixture_temp.name).resolve()
        sales, environment, cls.context = make_demo_context_data(cls.fixture)
        # Fit once; tests which change history exercise the real retraining path.
        cls.model = train(sales, environment)

    @classmethod
    def tearDownClass(cls):
        assert cls.fixture.is_relative_to(cls.scratch)
        cls.fixture_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.scratch)
        self.base = Path(self.temp.name).resolve()
        assert self.base.is_relative_to(self.scratch)
        data = prepare_demo(self.base, "2026-10-22")
        for name in ("sales.csv", "environment.json"):
            shutil.copyfile(self.fixture / name, data / name)
        self.instant = datetime(2026, 10, 22, 21, tzinfo=SHANGHAI)
        context = dict(self.context, forecast_origin=self.instant.isoformat())
        save_json(data / "target_context.json", context)
        self.service = CafeService(self.base, clock=lambda: self.instant)
        save_json(self.service.model_path, copy.deepcopy(self.model))
        save_json(self.service.runtime / "training_source.json",
                  self.service.training_stamp(self.service.inputs()))

    def tearDown(self):
        assert self.base.is_relative_to(self.scratch)
        self.temp.cleanup()

    def preview(self, proposal):
        return self.service.preview(proposal["id"], proposal["revision"],
                                    proposal["input_hash"], {})

    def approve(self, proposal):
        return self.service.approve(proposal["id"], proposal["revision"],
                                    proposal["input_hash"], {}, True)

    def weather(self, instant=None, rain=16.5):
        instant = instant or self.instant
        target = instant.date() + timedelta(days=1)
        payload = {
            "latitude": 3.125, "longitude": 101.6875,
            "timezone": "Asia/Kuala_Lumpur", "utc_offset_seconds": 28800,
            "daily_units": {"temperature_2m_mean": "°C", "precipitation_sum": "mm"},
            "daily": {"time": [target.isoformat()], "temperature_2m_mean": [28.7],
                      "precipitation_sum": [rain]},
        }
        return _parse_open_meteo(payload, target, instant, _weather_url(target))

    def update_context(self, **changes):
        path = self.service.data_dir / "target_context.json"
        context = load_json(path)
        context.update(changes)
        save_json(path, context)
        return context

    def refreshed_test_snapshot(self):
        context = self.update_context(precipitation_mm=17.25,
                                      known_at=self.instant.isoformat(),
                                      forecast_origin=self.instant.isoformat())
        return {"context": context}

    def schedule_count(self):
        with self.service.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM schedule_days").fetchone()[0]

    def test_weather_refresh_changes_proposal_without_retraining_and_blocks_old_approval(self):
        first = self.service.trigger()
        model_before = self.service.model_path.read_bytes()
        with patch("cafe_context.fetch_weather", return_value=self.weather()) as fetch, \
                patch("cafe_forecast_context.train", side_effect=AssertionError("weather alone must not retrain")):
            self.service.refresh_weather()
            second = self.service.trigger()
        fetch.assert_called_once_with("2026-10-23")
        self.assertEqual(self.service.model_path.read_bytes(), model_before)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["forecast"]["data_source"], "synthetic")
        self.assertEqual(second["forecast"]["context"]["weather_kind"], "live_forecast")
        self.assertEqual(self.service.store.proposal(first["id"])["status"], "superseded")
        for action in (self.preview, self.approve):
            with self.subTest(action=action.__name__), self.assertRaises(CafeError):
                action(first)
        self.assertFalse(self.service.store.snapshot()["orders"])

    def test_historical_environment_change_retrains_and_invalidates_old_proposal(self):
        first = self.service.trigger()
        path = self.service.data_dir / "environment.json"
        environment = load_json(path)
        environment[0]["temperature_c"] += 1
        save_json(path, environment)
        with self.assertRaises(CafeError):
            self.preview(first)
        self.assertFalse(self.service.context_state()["analysis_current"])
        with patch("cafe_forecast_context.train", wraps=train) as retrain:
            second = self.service.trigger()
        self.assertEqual(retrain.call_count, 1)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(load_json(self.service.model_path)["environment_sha256"],
                            self.model["environment_sha256"])
        self.assertEqual(load_json(self.service.runtime / "training_source.json"),
                         self.service.training_stamp(self.service.inputs()))

    def test_synthetic_weather_expiry_blocks_approval_without_any_file_change(self):
        self.update_context(known_at=(self.instant - timedelta(hours=47, minutes=30)).isoformat())
        proposal = self.service.trigger()
        source_hash = self.service.source_hash(self.service.inputs())
        self.instant += timedelta(hours=1)
        self.assertEqual(source_hash, self.service.source_hash(self.service.inputs()))
        state = self.service.context_state()
        self.assertEqual(state["status"], "needs_refresh")
        self.assertFalse(state["analysis_current"])
        for action in (self.preview, self.approve):
            with self.subTest(action=action.__name__), self.assertRaises(CafeError):
                action(proposal)
        self.assertFalse(self.service.store.snapshot()["orders"])

    def test_live_weather_has_stricter_24_hour_freshness_than_synthetic_training(self):
        with patch("cafe_context.fetch_weather", return_value=self.weather()):
            self.service.refresh_weather()
        proposal = self.service.trigger()
        self.instant += timedelta(hours=2)
        # Keep the same target date and make the live snapshot 25 hours old.
        old = self.instant - timedelta(hours=25)
        self.update_context(known_at=old.isoformat(), fetched_at=old.isoformat(),
                            forecast_origin=self.instant.isoformat())
        self.assertEqual(self.service.context_state()["status"], "needs_refresh")
        with self.assertRaises(CafeError):
            self.approve(proposal)

    def test_failed_weather_refresh_preserves_snapshot_and_records_failure(self):
        proposal = self.service.trigger()
        path = self.service.data_dir / "target_context.json"
        before = path.read_bytes()
        with patch("cafe_context.fetch_weather", side_effect=ContextUnavailable("offline")):
            with self.assertRaisesRegex(CafeError, "保留"):
                self.service.refresh_weather()
        self.assertEqual(path.read_bytes(), before)
        state = self.service.context_state()
        self.assertFalse(state["last_fetch"]["ok"])
        self.assertEqual(state["snapshot"]["weather_kind"], "synthetic_forecast")
        self.assertEqual(self.service.trigger()["id"], proposal["id"])

    def test_fetch_completion_time_can_be_later_than_refresh_start(self):
        def fetch(_target):
            self.instant += timedelta(seconds=1)
            return self.weather()
        with patch("cafe_context.fetch_weather", side_effect=fetch):
            result = self.service.refresh_weather()
        self.assertEqual(result["context"]["known_at"], self.instant.isoformat())
        self.assertEqual(self.service.context_state()["status"], "ready")
        self.assertEqual(self.service.trigger()["forecast"]["context"]["weather_kind"], "live_forecast")

    def test_calendar_unknown_is_not_treated_as_an_ordinary_business_day(self):
        unknown = {"is_holiday": None, "verified": False}
        with patch("cafe_context.holiday_for_day", return_value=unknown):
            self.assertEqual(self.service.context_state()["status"], "needs_refresh")
            with self.assertRaises(CafeError):
                self.service.trigger()
            before = (self.service.data_dir / "target_context.json").read_bytes()
            with patch("cafe_context.fetch_weather", return_value=self.weather()):
                with self.assertRaises(CafeError):
                    self.service.refresh_weather()
            self.assertEqual((self.service.data_dir / "target_context.json").read_bytes(), before)
        self.assertFalse(self.service.store.snapshot()["proposals"])

    def test_missing_one_context_input_cannot_silently_disable_context_model(self):
        (self.service.data_dir / "environment.json").unlink()
        with self.assertRaises(CafeError):
            self.service.inputs()
        with self.assertRaises(CafeError):
            self.service.trigger()

    def test_reading_current_context_never_fetches_weather(self):
        self.service.trigger()
        with patch("cafe_context.fetch_weather", side_effect=AssertionError("GET must not fetch")):
            for _ in range(3):
                state = self.service.context_state()
                self.assertTrue(state["analysis_current"])
                self.assertEqual(state["status"], "ready")

    def test_scenario_is_read_only_and_cannot_become_an_approvable_context(self):
        proposal = self.service.trigger()
        before = self.service.store.snapshot()
        path = self.service.data_dir / "target_context.json"
        context_before = path.read_bytes()
        inventory_before = self.service.inputs()["inventory"]
        result = self.service.scenario({"temperature_c": 31.5, "precipitation_mm": 1.0,
                                        "is_holiday": True})
        self.assertTrue(result["scenario"])
        self.assertFalse(result["persisted"])
        self.assertEqual(self.service.store.snapshot(), before)
        self.assertEqual(path.read_bytes(), context_before)
        self.assertEqual(self.service.inputs()["inventory"], inventory_before)
        self.preview(proposal)
        self.update_context(weather_kind="manual_scenario", is_holiday=True)
        with self.assertRaises(CafeError):
            self.service.trigger()
        with self.assertRaises(CafeError):
            self.approve(proposal)

    def test_approved_order_preserves_original_weather_and_analysis_after_refresh(self):
        proposal = self.service.trigger()
        inventory = self.service.inputs()["inventory"]
        order = self.approve(proposal)
        with patch("cafe_context.fetch_weather", return_value=self.weather(rain=25.0)):
            self.service.refresh_weather()
        stored = self.service.store.snapshot()["orders"][0]
        self.assertEqual(stored["analysis"], proposal["analysis"])
        self.assertEqual(stored["forecast"], proposal["forecast"])
        self.assertEqual(stored["id"], order["id"])
        self.assertFalse(stored["sent"])
        self.assertFalse(stored["inventory_changed"])
        self.assertEqual(self.service.inputs()["inventory"], inventory)
        drafts = self.service.store.drafts_for(stored["id"])
        self.assertTrue(drafts)
        self.assertTrue(all(draft["sent"] is False for draft in drafts))

    def test_22h_scheduler_refreshes_once_and_restart_does_not_refresh_again(self):
        self.instant = self.instant.replace(hour=21, minute=59)
        with patch.object(self.service, "refresh_weather", side_effect=self.refreshed_test_snapshot) as refresh:
            self.assertIsNone(self.service.scheduler_tick())
            refresh.assert_not_called()
            self.instant += timedelta(minutes=2)
            proposal = self.service.scheduler_tick()
            self.assertEqual(proposal["trigger"], "schedule")
            self.assertEqual(proposal["forecast"]["context"]["precipitation_mm"], 17.25)
            self.assertEqual(refresh.call_count, 1)
            self.assertIsNone(self.service.scheduler_tick())
            self.assertEqual(refresh.call_count, 1)
        restarted = CafeService(self.base, clock=lambda: self.instant)
        with patch.object(restarted, "refresh_weather", side_effect=AssertionError("completed day must not refetch")) as refresh:
            self.assertIsNone(restarted.scheduler_tick())
            refresh.assert_not_called()
        self.assertEqual(self.schedule_count(), 1)
        self.assertEqual(len(self.service.store.snapshot()["proposals"]), 1)

    def test_scheduler_failed_weather_refresh_leaves_day_incomplete_and_can_retry(self):
        self.instant = self.instant.replace(hour=22, minute=1)
        before = (self.service.data_dir / "target_context.json").read_bytes()
        with patch.object(self.service, "refresh_weather", side_effect=CafeError("weather offline")) as refresh:
            with self.assertRaisesRegex(CafeError, "offline"):
                self.service.scheduler_tick()
            refresh.assert_called_once_with()
        self.assertEqual(self.schedule_count(), 0)
        self.assertFalse(self.service.store.snapshot()["proposals"])
        self.assertEqual((self.service.data_dir / "target_context.json").read_bytes(), before)
        with patch.object(self.service, "refresh_weather", side_effect=self.refreshed_test_snapshot) as refresh:
            proposal = self.service.scheduler_tick()
            refresh.assert_called_once_with()
        self.assertEqual(proposal["trigger"], "schedule")
        self.assertEqual(self.schedule_count(), 1)
        self.assertEqual(len(self.service.store.snapshot()["proposals"]), 1)


if __name__ == "__main__":
    unittest.main()
