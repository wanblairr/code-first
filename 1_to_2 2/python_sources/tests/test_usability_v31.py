"""Before-confirmation validation and truthful daily-input readiness."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from cafe_app import prepare_demo
from cafe_core import CafeError, fingerprint, save_json
from cafe_service import CafeService, SHANGHAI, load_json

BASE = Path(__file__).resolve().parent.parent


class UsabilityTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.base = Path(self.temp.name)
        prepare_demo(self.base, "2026-10-22")
        self.instant = datetime(2026, 10, 22, 21, tzinfo=SHANGHAI)
        self.service = CafeService(self.base, clock=lambda: self.instant)
        shutil.copyfile(BASE / "models" / "demand_v3.json", self.service.model_path)
        save_json(self.service.runtime / "training_source.json", {"sales_hash": fingerprint(self.service.inputs()["sales"])})

    def tearDown(self):
        self.temp.cleanup()

    def preview(self, proposal, quantities=None, **kwargs):
        return self.service.preview(proposal["id"], kwargs.get("revision", proposal["revision"]),
                                    kwargs.get("input_hash", proposal["input_hash"]), quantities or {})

    def test_preview_reports_final_changes_and_shortage_without_writing(self):
        proposal = self.service.trigger()
        before = self.service.store.snapshot()
        inventory = self.service.inputs()["inventory"]
        preview = self.preview(proposal, {"coffee_beans": 4000, "milk": 18000})
        changes = {i["ingredient_id"]: i for i in preview["changes"]}
        self.assertEqual(changes["coffee_beans"]["suggested_quantity"], 3500)
        self.assertEqual(changes["coffee_beans"]["approved_quantity"], 4000)
        self.assertEqual(preview["supplier_count"], 3)
        self.assertTrue(any("牛奶" in warning and "未覆盖" in warning for warning in preview["warnings"]))
        self.assertEqual(self.service.store.snapshot(), before)
        self.assertEqual(self.service.inputs()["inventory"], inventory)

    def test_preview_invalid_quantity_and_stale_version_rejected(self):
        proposal = self.service.trigger()
        for quantities in ({"coffee_beans": -500}, {"coffee_beans": 3600}, {"coffee_beans": True}):
            with self.subTest(quantities=quantities), self.assertRaises(CafeError):
                self.preview(proposal, quantities)
        with self.assertRaises(CafeError):
            self.preview(proposal, revision=proposal["revision"] + 1)
        with self.assertRaises(CafeError):
            self.preview(proposal, revision=True)
        with self.assertRaises(CafeError):
            self.preview(proposal, input_hash="changed")
        self.assertFalse(self.service.store.snapshot()["orders"])

    def test_preview_inventory_change_or_crossday_rejected(self):
        proposal = self.service.trigger()
        path = self.service.data_dir / "inventory.json"
        inventory = load_json(path)
        inventory["snapshot_id"] = "new-count"
        save_json(path, inventory)
        with self.assertRaisesRegex(CafeError, "已有变化"):
            self.preview(proposal)
        self.instant += timedelta(days=1)
        with self.assertRaisesRegex(CafeError, "跨日"):
            self.preview(proposal)

    def test_preview_already_approved_and_second_day_draft_rejected(self):
        proposal = self.service.trigger()
        self.service.approve(proposal["id"], proposal["revision"], proposal["input_hash"], {}, True)
        with self.assertRaisesRegex(CafeError, "已经处理"):
            self.preview(proposal)
        inventory = self.service.inputs()["inventory"]
        inventory["snapshot_id"] = "later-count"
        save_json(self.service.data_dir / "inventory.json", inventory)
        later = self.service.trigger()
        with self.assertRaisesRegex(CafeError, "已经生成过采购草稿"):
            self.preview(later)
        self.assertEqual(len(self.service.store.snapshot()["orders"]), 1)

    def test_readiness_dates_and_missing_recipe_sku_are_truthful(self):
        inputs = self.service.inputs()
        ready = self.service.readiness(inputs)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["today"], "2026-10-22")
        self.assertEqual(ready["target_date"], "2026-10-23")
        self.assertEqual(ready["inventory"]["as_of_date"], "2026-10-22")
        self.assertNotIn("last_synced", ready)
        self.assertFalse(ready["sales"]["completion_verified"])
        self.assertIn("未验证已收盘", ready["sales"]["note"])
        inputs["inventory"]["as_of_date"] = "2026-10-21"
        self.assertEqual(self.service.readiness(inputs)["inventory"]["status"], "stale")
        inputs["sales"] = [r for r in inputs["sales"] if r["sku"] != "oat_latte"]
        stale = self.service.readiness(inputs)
        self.assertEqual(stale["sales"]["latest_date_by_sku"]["oat_latte"], None)
        self.assertEqual(stale["sales"]["status"], "stale")
        self.assertEqual(stale["status"], "needs_update")

    def test_rejection_keeps_optional_reason_in_log_and_supports_old_calls(self):
        proposal = self.service.trigger()
        self.service.store.reject(proposal["id"], proposal["revision"], "已有未收货订单，先核对供应商")
        snapshot = self.service.store.snapshot()
        self.assertEqual(snapshot["proposals"][0]["status"], "rejected")
        event = next(e for e in snapshot["events"] if e["action"] == "proposal_rejected")
        self.assertEqual(json.loads(event["detail"])["reason"], "已有未收货订单，先核对供应商")
        inventory = self.service.inputs()["inventory"]
        inventory["snapshot_id"] = "new-proposal"
        save_json(self.service.data_dir / "inventory.json", inventory)
        second = self.service.trigger()
        with self.assertRaises(CafeError):
            self.service.store.reject(second["id"], second["revision"], 123)
        self.service.store.reject(second["id"], second["revision"])
        self.assertEqual(self.service.store.proposal(second["id"])["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
