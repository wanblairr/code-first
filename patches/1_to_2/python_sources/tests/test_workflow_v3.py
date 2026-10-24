"""Persistent end-to-end replenishment invariants, with isolated demo stores."""
import copy
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path

from cafe_app import prepare_demo
from cafe_core import CafeError, fingerprint, save_json
from cafe_service import CafeService, SHANGHAI, load_json


BASE = Path(__file__).resolve().parent.parent


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(dir=self.scratch)
        self.base = Path(self.temp.name).resolve()
        assert self.base.is_relative_to(self.scratch)
        prepare_demo(self.base, "2026-10-22")
        self.instant = datetime(2026, 10, 22, 22, 1, tzinfo=SHANGHAI)
        self.service = CafeService(self.base, clock=lambda: self.instant)
        shutil.copyfile(BASE/"models"/"demand_v3.json", self.service.model_path)
        save_json(self.service.runtime/"training_source.json",
                  {"sales_hash": fingerprint(self.service.inputs()["sales"])})

    def tearDown(self):
        assert self.base.is_relative_to(self.scratch)
        self.temp.cleanup()

    def proposal(self):
        return self.service.trigger()

    def approve(self, proposal, quantities=None, acknowledged=True):
        return self.service.approve(proposal["id"], proposal["revision"],
                                    proposal["input_hash"], quantities or {}, acknowledged)

    def modify_inventory(self, callback):
        path = self.service.data_dir/"inventory.json"
        inventory = load_json(path)
        callback(inventory)
        save_json(path, inventory)

    def count(self, table):
        self.assertIn(table, ("proposals", "orders", "drafts", "schedule_days"))
        with self.service.store.connect() as db:
            return db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]

    def test_repeat_trigger_is_idempotent_and_has_todays_sales(self):
        first, second = self.proposal(), self.service.trigger("schedule")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["target_date"], "2026-10-23")
        self.assertEqual(first["plan"]["as_of_date"], "2026-10-22")
        self.assertEqual({i["history_through"] for i in first["forecast"]["items"]}, {"2026-10-22"})
        self.assertEqual(self.count("proposals"), 1)
        events = self.service.store.snapshot()["events"]
        self.assertEqual(sum(e["action"] == "approval_requested" for e in events), 1)

    def test_owner_edits_appear_in_draft_skip_warning_persists_no_stock_increase(self):
        proposal = self.proposal()
        before = self.service.inputs()["inventory"]
        order = self.approve(proposal, {"coffee_beans": 4000, "milk": 0})
        items = {i["ingredient_id"]: i for i in order["items"]}
        self.assertEqual(items["coffee_beans"]["approved_quantity"], 4000)
        self.assertNotEqual(items["coffee_beans"]["suggested_quantity"], 4000)
        self.assertEqual(items["milk"]["approved_quantity"], 0)
        self.assertTrue(items["milk"]["approval_warnings"])
        self.assertGreater(items["milk"]["shortage"], 0)
        drafts = self.service.store.drafts_for(order["id"])
        bodies = "\n".join(d["body"] for d in drafts)
        self.assertIn("4000 g", bodies)
        self.assertNotIn("- 牛奶：", bodies)
        self.assertTrue(all(d["sent"] is False for d in drafts))
        self.assertTrue(all("X-Unsent: 1" in d["email_eml"] for d in drafts))
        self.assertFalse(order["inventory_changed"])
        self.assertFalse(order["sent"])
        self.assertEqual(before, self.service.inputs()["inventory"])
        stored = self.service.store.snapshot()["orders"][0]
        self.assertEqual(stored["items"], order["items"])
        self.assertEqual(self.service.store.proposal(proposal["id"])["status"], "approved")
        self.assertTrue(any(e["action"] == "approved_draft_created" for e in self.service.store.snapshot()["events"]))

    def test_same_day_second_order_blocked_transactionally(self):
        first = self.proposal()
        self.approve(first)
        self.modify_inventory(lambda d: d.update(snapshot_id="updated-after-first-approval"))
        second = self.proposal()
        before_drafts = self.count("drafts")
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(CafeError):
            self.approve(second)
        self.assertEqual(self.count("orders"), 1)
        self.assertEqual(self.count("drafts"), before_drafts)
        self.assertEqual(self.service.store.proposal(second["id"])["status"], "pending")
        with self.assertRaises(CafeError):
            self.approve(first)

    def test_changed_snapshot_stale_approval_and_superseded_version_blocked(self):
        first = self.proposal()
        self.modify_inventory(lambda d: d.update(snapshot_id="replacement-closing-snapshot"))
        with self.assertRaises(CafeError):
            self.approve(first)
        self.assertEqual(self.count("orders"), 0)
        replacement = self.proposal()
        stale = self.service.store.proposal(first["id"])
        self.assertEqual(stale["status"], "superseded")
        self.assertGreater(stale["revision"], first["revision"])
        self.assertNotEqual(replacement["id"], first["id"])
        with self.assertRaises(CafeError):
            self.approve(first)

    def test_midnight_expires_previous_days_approval(self):
        proposal = self.proposal()
        self.instant = datetime(2026, 10, 23, 0, 1, tzinfo=SHANGHAI)
        with self.assertRaises(CafeError):
            self.approve(proposal)
        self.assertEqual(self.count("orders"), 0)

    def test_owner_acknowledgment_revision_and_invalid_quantity_are_required(self):
        proposal = self.proposal()
        with self.assertRaises(CafeError):
            self.approve(proposal, acknowledged=False)
        for overrides in ({"milk": -1}, {"milk": 1001}, {"milk": float("nan")},
                          {"unknown_ingredient": 1}, {i["ingredient_id"]: 0 for i in proposal["plan"]["items"]}):
            with self.subTest(overrides=overrides), self.assertRaises(CafeError):
                self.approve(proposal, overrides)
        with self.assertRaises(CafeError):
            self.service.approve(proposal["id"], proposal["revision"]+1, proposal["input_hash"], {}, True)
        self.assertEqual(self.count("orders"), 0)
        self.assertEqual(self.count("drafts"), 0)

    def test_scheduler_before22_then_deduplicates_across_restart(self):
        self.instant = datetime(2026, 10, 22, 21, 59, tzinfo=SHANGHAI)
        self.assertIsNone(self.service.scheduler_tick())
        self.assertEqual(self.count("proposals"), 0)
        self.instant += timedelta(minutes=2)
        first = self.service.scheduler_tick()
        self.assertEqual(first["trigger"], "schedule")
        self.assertEqual(self.count("schedule_days"), 1)
        restarted = CafeService(self.base, clock=lambda: self.instant)
        self.assertIsNone(restarted.scheduler_tick())
        self.assertEqual(self.count("proposals"), 1)
        completed = [e for e in self.service.store.snapshot()["events"] if e["action"] == "scheduled_trigger_completed"]
        self.assertEqual(len(completed), 1)

    def test_failed_scheduler_not_marked_complete_can_retry(self):
        self.modify_inventory(lambda d: d.update(as_of_date="2026-10-21"))
        with self.assertRaises(CafeError):
            self.service.scheduler_tick()
        self.assertEqual(self.count("schedule_days"), 0)
        self.assertEqual(self.count("proposals"), 0)
        self.modify_inventory(lambda d: d.update(as_of_date="2026-10-22"))
        self.assertIsNotNone(self.service.scheduler_tick())
        self.assertEqual(self.count("schedule_days"), 1)

    def test_invalid_inventory_unit_never_saves_a_proposal(self):
        self.modify_inventory(lambda d: d["items"][0].update(unit="kg"))
        with self.assertRaises(CafeError):
            self.proposal()
        self.assertEqual(self.count("proposals"), 0)
        self.assertEqual(self.count("orders"), 0)

    def test_stockout_zeros_batches_marks_affected_sku_and_excludes_new_label(self):
        original = load_json(self.service.model_path)
        proposal = self.service.stockout("oat_milk")
        updated = self.service.inputs()
        ingredient = next(i for i in updated["inventory"]["items"] if i["ingredient_id"] == "oat_milk")
        self.assertTrue(all(b["quantity"] == 0 for b in ingredient["batches"]))
        self.assertEqual(ingredient["inbounds"][0]["quantity"], 1000)
        today = {r["sku"]: r for r in updated["sales"] if r["date"] == "2026-10-22"}
        self.assertTrue(today["oat_latte"]["stockout"])
        self.assertFalse(today["espresso"]["stockout"])
        self.assertFalse(today["latte"]["stockout"])
        retrained = load_json(self.service.model_path)
        self.assertEqual(retrained["evaluation"]["oat_latte"]["excluded_stockout_labels"],
                         original["evaluation"]["oat_latte"]["excluded_stockout_labels"]+1)
        self.assertEqual(retrained["models"]["oat_latte"]["fit"]["fitted_rows"],
                         original["models"]["oat_latte"]["fit"]["fitted_rows"]-1)
        self.assertTrue(proposal["forecast"]["warnings"])
        self.assertEqual(proposal["trigger"], "stockout")
        snapshot = self.service.store.snapshot()
        self.assertEqual(sum(e["action"] == "stockout_recorded" for e in snapshot["events"]), 1)
        repeat = self.service.stockout("oat_milk")
        self.assertEqual(repeat["id"], proposal["id"])
        self.assertEqual(sum(e["action"] == "stockout_recorded" for e in self.service.store.snapshot()["events"]), 1)

    def test_stockout_writeahead_journal_recovers_both_inputs_once_after_crash(self):
        journal = self.service.runtime/"pending_stockout.json"
        inventory_before = self.service.inputs()["inventory"]
        original_recovery = self.service._recover_stockout
        def crash_after_journal():
            if journal.exists():
                raise RuntimeError("simulated crash after stockout journal commit")
            return original_recovery()
        with patch.object(self.service, "_recover_stockout", side_effect=crash_after_journal):
            with self.assertRaises(RuntimeError):
                self.service.stockout("oat_milk")
        self.assertTrue(journal.exists())
        pending = load_json(journal)
        self.assertEqual(load_json(self.service.data_dir/"inventory.json"), inventory_before)
        self.assertEqual(self.count("proposals"), 0)
        restarted = CafeService(self.base, clock=lambda: self.instant)
        recovered = restarted.inputs()
        self.assertFalse(journal.exists())
        ingredient = next(i for i in recovered["inventory"]["items"] if i["ingredient_id"] == "oat_milk")
        self.assertTrue(all(b["quantity"] == 0 for b in ingredient["batches"]))
        self.assertTrue(next(r for r in recovered["sales"] if r["date"] == "2026-10-22" and r["sku"] == "oat_latte")["stockout"])
        self.assertEqual(recovered["inventory"]["snapshot_id"], pending["inventory"]["snapshot_id"])
        # Replaying a journal left behind after its database event was committed
        # must not duplicate that event or increase inventory on a later read.
        save_json(journal, pending)
        self.assertEqual(restarted.inputs(), recovered)
        self.assertEqual(restarted.inputs(), recovered)
        self.assertFalse(journal.exists())
        events = restarted.store.snapshot()["events"]
        matching = [e for e in events if e["action"] == "stockout_recorded"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["subject"], pending["event_id"])


if __name__ == "__main__":
    unittest.main()
