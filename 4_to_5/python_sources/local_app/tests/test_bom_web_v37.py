"""Real-loopback v3.7 history/extras authentication and persistent purchase flow."""
from copy import deepcopy
import unittest
import test_bom_web_v36 as legacy
from cafe_core import CafeError


class V37HTTPTests(unittest.TestCase):
    setUp = legacy.BOMWebTests.setUp
    tearDown = legacy.BOMWebTests.tearDown
    start_server = legacy.BOMWebTests.start_server
    stop_server = legacy.BOMWebTests.stop_server
    request = legacy.BOMWebTests.request
    login = legacy.BOMWebTests.login
    rain = legacy.BOMWebTests.rain

    @staticmethod
    def day(revision=0):
        return {"revision":revision,"date":"2026-10-22","rows":[
            {"productId":"BV-013","size":"中杯","quantity":70},
            {"productId":"BV-003","size":"中杯","quantity":35}],"request_id":"http-history-v37"}

    @staticmethod
    def extras(revision=0):
        return {"revision":revision,"items":[{"id":"custom_http_cups","name":"纸杯（HTTP验收）",
            "quantity":2,"unit":"box","unitPriceCents":1025}],"request_id":"http-extras-v37"}

    def test_new_mutations_require_password_session_and_csrf(self):
        for route, body in (("history",self.day()),("extras",self.extras())):
            self.assertEqual(self.request("POST","/api/bom/"+route,body)[0],401)
        self.login()
        for route, body in (("history",self.day()),("extras",self.extras())):
            self.assertEqual(self.request("POST","/api/bom/"+route,body,{"X-CSRF-Token":"invalid"})[0],403)
        state = self.service.bom_state()
        self.assertEqual(state["sales_history"]["revision"],0)
        self.assertEqual(state["extras"]["revision"],0)

    def test_saved_history_custom_prices_approval_download_and_restart(self):
        self.login()
        self.rain()
        stock = self.service.bom_state()["stock"]
        status, _, saved = self.request("POST","/api/bom/history",self.day())
        self.assertEqual(status,200,saved)
        self.assertEqual(saved["stock"],stock)
        self.assertEqual(saved["as_of_date"],"2026-10-23")
        self.assertEqual(saved["history_result"]["request_id"],"http-history-v37")
        self.assertAlmostEqual(next(row for row in saved["popularity"]["products"] if row["productId"]=="BV-013")["weight"],2/3)
        status, _, saved = self.request("POST","/api/bom/history",self.day(1))
        self.assertEqual(status,200,saved)
        self.assertEqual(saved["sales_history"]["revision"],1)
        status, _, saved = self.request("POST","/api/bom/extras",self.extras())
        self.assertEqual(status,200,saved)
        self.assertEqual(saved["extras_result"]["request_id"],"http-extras-v37")
        status, _, result = self.request("POST","/api/bom/prices",{"revision":0,"entries":[
            {"ingredient_id":"INV-005","pack_quantity":1000,"pack_price_cents":1200},
            {"ingredient_id":"INV-003","pack_quantity":1000,"pack_price_cents":800}]})
        self.assertEqual(status,200,result)
        status, _, suggested = self.request("POST","/api/bom/suggest",{})
        self.assertEqual(status,200,suggested)
        p = suggested["proposal"]
        self.assertEqual(p["extras"]["totalCents"],2050)
        args = {"proposal_id":p["id"],"revision":p["revision"],"input_hash":p["input_hash"],
                "quantities":{"custom_http_cups":3},"acknowledged":True}
        status, _, preview = self.request("POST","/api/bom/preview",args)
        self.assertEqual(status,200,preview)
        self.assertEqual(preview["extras_total_cents"],3075)
        status, _, approved = self.request("POST","/api/bom/approve",args)
        self.assertEqual(status,200,approved)
        order = approved["orders"][0]
        self.assertEqual(order["total_cents"],preview["total_cents"])
        draft_id = order["drafts"][0]["id"]
        for suffix in ("txt","eml"):
            status, headers, content = self.request("GET","/api/bom/drafts/"+draft_id+"?format="+suffix)
            self.assertEqual(status,200)
            self.assertIn("attachment",headers["Content-Disposition"])
            if suffix == "txt":
                self.assertIn("纸杯（HTTP验收）: 3 box",content)
                self.assertIn("MYR 30.75",content)
        self.stop_server()
        self.start_server()
        self.login()
        status, _, reloaded = self.request("GET","/api/bom/state")
        self.assertEqual(status,200)
        self.assertEqual(reloaded["stock"],stock)
        self.assertEqual(reloaded["sales_history"],saved["sales_history"])
        self.assertEqual(reloaded["extras"]["revision"],1)
        self.assertEqual(reloaded["active_order_id"],order["id"])
        self.assertEqual(reloaded["orders"][0]["total_cents"],preview["total_cents"])

    def test_past_date_and_cas_validation_and_history_invalidates_old_approval(self):
        self.login()
        self.rain()
        today = self.day()
        today["date"] = "2026-10-23"
        self.assertEqual(self.request("POST","/api/bom/history",today)[0],400)
        self.assertEqual(self.request("POST","/api/bom/history",self.day())[0],200)
        self.assertEqual(self.request("POST","/api/bom/history",self.day())[0],400)
        self.assertEqual(self.request("POST","/api/bom/extras",self.extras())[0],200)
        self.assertEqual(self.request("POST","/api/bom/extras",self.extras())[0],400)
        status, _, suggested = self.request("POST","/api/bom/suggest",{})
        self.assertEqual(status,200,suggested)
        p = suggested["proposal"]
        changed = self.day(1)
        changed["rows"][0]["quantity"] = 10
        self.assertEqual(self.request("POST","/api/bom/history",changed)[0],200)
        status, _, result = self.request("POST","/api/bom/approve",{
            "proposal_id":p["id"],"revision":p["revision"],"input_hash":p["input_hash"],
            "quantities":{},"acknowledged":True})
        self.assertEqual(status,400,result)
        self.assertFalse(self.service.bom_state()["orders"])


if __name__ == "__main__":
    unittest.main()
