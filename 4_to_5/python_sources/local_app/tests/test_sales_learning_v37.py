"""v3.7 server-authoritative historical sales, custom purchases and durable orders."""
from copy import deepcopy
from datetime import datetime
from email import policy
from email.parser import Parser
from pathlib import Path
import os
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cafe_core import CafeError
from cafe_service import CafeService, SHANGHAI
from cafe_bom import load_bom_data, forecast_menu
from cafe_bom_context import forecast_context
from cafe_sales_learning import (normalize_sales_day, normalize_history, upsert_sales_day,
    build_popularity, forecast_from_history, normalize_extra_purchases, price_extra_purchases)

BASE = Path(__file__).resolve().parent.parent
TODAY = "2026-10-24"
DAY = {"date": "2026-10-23", "rows": [
    {"productId": "BV-013", "size": "中杯", "quantity": 70},
    {"productId": "BV-003", "size": "中杯", "quantity": 35}]}
EXTRA = {"id": "custom_cups", "name": "纸杯（验收示例）", "quantity": 2,
         "unit": "box", "unitPriceCents": 1025}


class SalesLearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_bom_data()

    def history(self, days=None):
        return {"revision": 1, "days": deepcopy(days if days is not None else [DAY])}

    def test_yesterday_weights_are_two_thirds_and_one_third_separate_from_bakery(self):
        result = build_popularity(self.data, self.history(), TODAY)
        rows = {row["productId"]: row for row in result["products"]}
        self.assertAlmostEqual(rows["BV-013"]["weight"], 2/3)
        self.assertAlmostEqual(rows["BV-003"]["weight"], 1/3)
        self.assertEqual(rows["BV-013"]["rank"], 1)
        self.assertEqual(rows["BK-01"]["weight"], 0)
        self.assertEqual(result["sampleDays"], 1)
        self.assertEqual(result["confidence"], "low")

    def test_exact_seven_day_gap_halves_weight_and_omitted_variant_is_zero(self):
        days = [{"date": "2026-10-16", "rows": [{"productId": "BV-013", "size": "中杯", "quantity": 30}]},
                {"date": "2026-10-23", "rows": [{"productId": "BV-003", "size": "中杯", "quantity": 30}]}]
        result = build_popularity(self.data, self.history(days), TODAY)
        rows = {(row["productId"], row["size"]): row for row in result["sizes"]}
        self.assertEqual(rows[("BV-013", "中杯")]["weightedUnits"], 15)
        self.assertEqual(rows[("BV-013", "中杯")]["dailyAverage"], 10)
        self.assertEqual(rows[("BV-003", "中杯")]["dailyAverage"], 20)
        self.assertAlmostEqual(rows[("BV-003", "中杯")]["weight"], 2/3)

    def test_empty_complete_day_means_zero_not_synthetic_fallback(self):
        result = build_popularity(self.data, self.history([{"date": "2026-10-23", "rows": []}]), TODAY)
        self.assertEqual(result["sampleDays"], 1)
        self.assertTrue(all(row["dailyAverage"] == 0 for row in result["sizes"]))

    def test_old_valid_days_do_not_expire_and_common_scaling_avoids_underflow(self):
        days = [{"date": "2000-01-01", "rows": DAY["rows"]}]
        result = build_popularity(self.data, self.history(days), TODAY)
        rows = {row["productId"]: row for row in result["products"]}
        self.assertEqual(rows["BV-013"]["dailyAverage"], 70)
        self.assertGreater(result["staleDays"], 9000)

    def test_same_date_replaces_atomically_and_noop_does_not_increment(self):
        first = upsert_sales_day(self.data, {"revision":0,"days":[]}, DAY, TODAY, 0)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(upsert_sales_day(self.data, first, DAY, TODAY, 1), first)
        changed = deepcopy(DAY)
        changed["rows"][0]["quantity"] = 8
        second = upsert_sales_day(self.data, first, changed, TODAY, 1)
        self.assertEqual(len(second["days"]), 1)
        self.assertEqual(second["revision"], 2)
        with self.assertRaises(CafeError):
            upsert_sales_day(self.data, second, DAY, TODAY, 1)

    def test_dates_rows_numbers_and_duplicate_variants_fail_closed(self):
        cases = [{"date":TODAY,"rows":[]}, {"date":"2026-02-30","rows":[]},
                 {"date":"2026-10-25","rows":[]}, {"date":"2026-10-23","rows":DAY["rows"]*2},
                 {"date":"2026-10-23","rows":[{"productId":"missing","size":"中杯","quantity":1}]}]
        for bad in (True, -1, 0.5, 100001, "3"):
            cases.append({"date":"2026-10-23","rows":[dict(DAY["rows"][0], quantity=bad)]})
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(CafeError):
                normalize_sales_day(self.data, bad, TODAY)

    def test_history_duplicate_dates_and_excess_days_rejected(self):
        with self.assertRaises(CafeError):
            normalize_history(self.data, self.history([DAY, DAY]), TODAY)
        with self.assertRaises(CafeError):
            normalize_history(self.data, self.history([DAY]*367), TODAY)

    def test_custom_cent_prices_limits_zero_and_visible_names(self):
        priced = price_extra_purchases(self.data, [EXTRA])
        self.assertEqual(priced["totalCents"], 2050)
        self.assertEqual(price_extra_purchases(self.data, [dict(EXTRA,unitPriceCents=0)])["totalCents"], 0)
        for invalid in [dict(EXTRA, unitPriceCents=10.25), dict(EXTRA, quantity=True),
                        dict(EXTRA, id="INV-005"), dict(EXTRA, name="bad\nname"),
                        dict(EXTRA, unit="arbitrary"), dict(EXTRA, quantity=10001),
                        dict(EXTRA, unitPriceCents=1000001)]:
            with self.subTest(invalid=invalid), self.assertRaises(CafeError):
                normalize_extra_purchases(self.data, [invalid])
        with self.assertRaises(CafeError):
            normalize_extra_purchases(self.data, [EXTRA, EXTRA])
        with self.assertRaises(CafeError):
            normalize_extra_purchases(self.data, [dict(EXTRA,id="custom_"+str(i)) for i in range(21)])


class LocalV37WorkflowTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No live APIs"))
        network.start()
        self.addCleanup(network.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="cafe-v37-")
        self.base = Path(self.tmp.name)
        self.instant = datetime(2026,10,24,1,30,tzinfo=SHANGHAI)
        self.service = CafeService(self.base, clock=lambda:self.instant)
        self.service.bom_enabled = True
        self.app = self.service.bom()
        self.app.set_context({"weather_mode":"rain","holiday_mode":"ordinary"})

    def tearDown(self):
        self.tmp.cleanup()

    def save_day(self, day=DAY, revision=0, request_id="v37-history"):
        return self.app.set_history({"date":day["date"],"rows":deepcopy(day["rows"]),
                                     "revision":revision,"request_id":request_id})

    def save_extra(self, items=None, revision=0):
        return self.app.set_extras({"revision":revision,"items":deepcopy(items if items is not None else [EXTRA]),
                                   "request_id":"v37-extra-"+str(revision)})

    def quote(self):
        return self.app.set_prices({"revision":0,"entries":[{
            "ingredient_id":"INV-005","pack_quantity":1000,"pack_price_cents":1200,
            "currency":"MYR","source":"owner_entry"}]})

    @staticmethod
    def approval(p, quantities=None):
        return {"proposal_id":p["id"],"revision":p["revision"],"input_hash":p["input_hash"],
                "quantities":quantities or {},"acknowledged":True}

    def test_history_does_not_deduct_stock_or_change_sale_undo_candidate(self):
        before = self.app.sale({"productId":"BV-013","size":"中杯","quantity":1,"revision":0,"request_id":"one"})
        after = self.save_day()
        self.assertEqual(after["stock"],before["stock"])
        self.assertEqual(after["revision"],before["revision"])
        self.assertEqual(after["last_transaction"],before["last_transaction"])
        self.assertEqual(after["history_result"]["request_id"],"v37-history")
        self.assertEqual(after["as_of_date"],TODAY)
        event = next(row for row in after["events"] if row["action"]=="bom_history_saved")
        self.assertFalse(event["payload"]["inventory_changed"])

    def test_no_history_forecast_preserves_synthetic_fallback(self):
        state = self.app.suggest()
        self.assertEqual(state["proposal"]["forecast"],forecast_menu(self.app.data,self.app.context()))
        self.assertEqual(state["popularity"]["sampleDays"],0)

    def test_history_forecast_uses_weather_coefficient_once_and_all_sizes(self):
        self.save_day()
        state = self.app.suggest()
        forecast = state["proposal"]["forecast"]
        self.assertEqual(forecast["source"],"owner_sales_recency_weighted_v1")
        oat = next(row for row in forecast["sales"] if row["productId"]=="BV-013" and row["size"]=="中杯")
        self.assertEqual(oat["quantity"],__import__("math").ceil(70*forecast["coefficient"]))
        self.assertEqual(next(row for row in forecast["sales"] if row["productId"]=="BV-013" and row["size"]=="大杯")["quantity"],0)

    def test_combined_order_6850_cents_downloads_and_provider_view_are_consistent(self):
        self.save_day()
        self.save_extra()
        self.quote()
        p = self.app.suggest()["proposal"]
        self.assertEqual(p["pricing"]["total_cents"],6850)
        self.assertEqual(p["extras"]["totalCents"],2050)
        values = self.approval(p)
        preview = self.app.preview(values)
        self.assertEqual(preview["total_cents"],6850)
        state = self.app.approve(values)
        order = state["orders"][0]
        self.assertEqual(order["total_cents"],6850)
        self.assertEqual(order["extras_total_cents"],2050)
        self.assertEqual(next(row for row in order["items"] if row.get("item_type")=="custom_purchase")["name"],EXTRA["name"])
        draft_id = order["drafts"][0]["id"]
        draft, transport = self.service.bom_approved_draft(draft_id)
        self.assertIn("Estimated total: MYR 68.50",draft["body"])
        self.assertIn(EXTRA["name"],draft["body"])
        self.assertIn("MYR 10.25/box = MYR 20.50",draft["body"])
        message = Parser(policy=policy.default).parsestr(draft["email_eml"])
        self.assertIn(EXTRA["name"],message.get_content())
        extra = next(row for row in transport["items"] if row.get("item_type")=="custom_purchase")
        self.assertEqual(extra["purchase_unit"],"box")
        self.assertEqual(extra["approved_quantity"],2)
        self.assertEqual(extra["unit_price_cents"],1025)
        again = self.app.approve(values)
        self.assertEqual(again["orders"][0]["id"],order["id"])
        self.assertEqual(len(again["orders"]),1)

    def test_custom_purchase_quantity_can_be_changed_and_total_recomputed(self):
        self.save_day({"date":"2026-10-23","rows":[]})
        self.save_extra()
        p = self.app.suggest()["proposal"]
        values = self.approval(p,{"custom_cups":3})
        self.assertEqual(self.app.preview(values)["total_cents"],3075)
        order = self.app.approve(values)["orders"][0]
        self.assertEqual(order["total_cents"],3075)
        self.assertEqual(next(row for row in order["items"] if row.get("item_type")=="custom_purchase")["approved_quantity"],3)
        with self.assertRaises(CafeError):
            self.app.approve(self.approval(p,{"custom_cups":4}))

    def test_unknown_bom_prices_keep_total_pending_but_custom_known_subtotal(self):
        self.save_day()
        self.save_extra()
        p = self.app.suggest()["proposal"]
        self.assertIsNone(p["pricing"]["total_cents"])
        self.assertEqual(p["pricing"]["known_subtotal_cents"],2050)
        order = self.app.approve(self.approval(p))["orders"][0]
        self.assertIsNone(order["total_cents"])
        self.assertFalse(order["prices_complete"])
        self.assertIn("Estimated total: unconfirmed",self.app.draft(order["drafts"][0]["id"])["body"])

    def test_history_and_extras_survive_restart_and_same_save_noops(self):
        saved = self.save_day()
        self.save_extra()
        p = self.app.suggest()["proposal"]
        unchanged = self.save_day(revision=1)
        self.assertEqual(unchanged["sales_history"]["revision"],1)
        self.assertEqual(unchanged["proposal"]["id"],p["id"])
        self.save_extra(revision=1)
        other = CafeService(self.base,clock=lambda:self.instant)
        reloaded = other.bom_state()
        self.assertEqual(reloaded["sales_history"],saved["sales_history"])
        self.assertEqual(reloaded["extras"]["items"],[EXTRA])
        self.assertEqual(reloaded["extras"]["revision"],1)

    def test_changed_history_invalidates_proposal_and_external_old_order(self):
        p = self.app.suggest()["proposal"]
        self.save_day()
        with self.assertRaises(CafeError):
            self.app.approve(self.approval(p))
        p = self.app.suggest()["proposal"]
        order = self.app.approve(self.approval(p))["orders"][0]
        self.save_day(dict(DAY,rows=[dict(DAY["rows"][0],quantity=40)]),revision=1)
        with self.assertRaises(CafeError):
            self.service.bom_approved_draft(order["drafts"][0]["id"])

    def test_changed_extras_price_and_change_back_cannot_revive_old_order(self):
        self.save_day()
        self.save_extra()
        p = self.app.suggest()["proposal"]
        order = self.app.approve(self.approval(p))["orders"][0]
        self.save_extra([dict(EXTRA,unitPriceCents=1026)],1)
        self.save_extra([EXTRA],2)
        self.assertIsNone(self.app.state()["active_order_id"])
        with self.assertRaises(CafeError):
            self.service.bom_approved_draft(order["drafts"][0]["id"])
        self.assertNotEqual(self.app.suggest()["proposal"]["input_hash"],p["input_hash"])

    def test_conflicting_history_and_extras_revisions_fail_without_stock_change(self):
        self.save_day()
        self.save_extra()
        before = self.app.state()
        with self.assertRaises(CafeError):
            self.save_day(revision=0)
        with self.assertRaises(CafeError):
            self.save_extra(revision=0)
        after = self.app.state()
        self.assertEqual(after["stock"],before["stock"])
        self.assertEqual(after["sales_history"],before["sales_history"])
        self.assertEqual(after["extras"],before["extras"])

    def test_audit_failure_rolls_back_history_and_extras_and_proposal_status(self):
        p = self.app.suggest()["proposal"]
        with patch.object(self.service.store,"event",side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.save_day()
            with self.assertRaises(RuntimeError):
                self.save_extra()
        after = self.app.state()
        self.assertEqual(after["sales_history"]["revision"],0)
        self.assertEqual(after["extras"]["revision"],0)
        self.assertEqual(after["proposal"]["id"],p["id"])

    def test_concurrent_extra_writers_have_one_cas_winner(self):
        gate = threading.Barrier(2)
        def run(amount):
            gate.wait()
            try:
                return self.app.repo.learning.save_extras([dict(EXTRA,quantity=amount)],0)
            except CafeError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run,[2,3]))
        self.assertEqual(sum(value is not None for value in results),1)
        self.assertEqual(self.app.state()["extras"]["revision"],1)

    def test_server_rejects_today_and_invalid_payload_before_persistence(self):
        with self.assertRaises(CafeError):
            self.save_day({"date":TODAY,"rows":[]})
        with self.assertRaises(CafeError):
            self.app.set_history({"revision":0,"day":DAY,"date":"2026-10-23"})
        with self.assertRaises(CafeError):
            self.app.set_extras({"revision":0,"items":[dict(EXTRA,unitPriceCents=1.5)]})
        self.assertEqual(self.app.state()["sales_history"]["revision"],0)
        self.assertEqual(self.app.state()["extras"]["revision"],0)


    def test_gmail_and_whatsapp_previews_include_same_custom_lines_and_total_without_network(self):
        self.save_day()
        self.save_extra()
        self.quote()
        p = self.app.suggest()["proposal"]
        order = self.app.approve(self.approval(p))["orders"][0]
        draft_id = order["drafts"][0]["id"]
        cloud = self.service._cloud()
        with patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("No live APIs")):
            cloud.config.update({"gmail_access_token":"ya29.v37-fixture-only"})
            gmail = Mock()
            gmail.create_draft.return_value = {"gmail_draft_id":"v37-fixture","status":"draft","sent":False}
            cloud.gmail_factory = Mock(return_value=gmail)
            result = cloud.export_gmail(draft_id)
            self.assertEqual(result["status"],"created")
            draft = gmail.create_draft.call_args.args[0]
            self.assertIn(EXTRA["name"],draft["body"])
            self.assertIn("MYR 68.50",draft["body"])
            whatsapp = cloud.whatsapp_api()
            _, _, parameters = whatsapp._approved(draft_id)
            self.assertIn(EXTRA["name"],parameters[2])
            self.assertIn("MYR 68.50",parameters[2])
            _, _, body = cloud.whatsapp_sandbox()._approved(draft_id)
            self.assertIn(EXTRA["name"],body)
            self.assertIn("MYR 68.50",body)
            self.assertEqual(self.app.state()["stock"]["INV-005"],8000)


if __name__ == "__main__":
    unittest.main()
