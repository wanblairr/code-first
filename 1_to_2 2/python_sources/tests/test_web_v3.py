"""Real localhost HTTP tests for owner-only review and transactional drafts."""
import concurrent.futures
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from cafe_app import BASE, prepare_demo
from cafe_auth import make_auth_policy
from cafe_core import fingerprint, save_json
from cafe_service import CafeService, SHANGHAI
from cafe_web import make_server

TEST_PASSWORD = "web-fixture-test-password"


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        prepare_demo(self.base, "2026-10-22")
        self.service = CafeService(self.base, clock=lambda: datetime(2026, 10, 22, 21, tzinfo=SHANGHAI))
        shutil.copyfile(BASE / "models" / "demand_v3.json", self.service.model_path)
        save_json(self.service.runtime / "training_source.json", {"sales_hash": fingerprint(self.service.inputs()["sales"])})
        password_config = self.service.runtime / "test_auth.json"
        save_json(password_config, make_auth_policy(TEST_PASSWORD))
        self.server = make_server(self.service, port=0, password_config=password_config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        common = {"Content-Type": "application/json", "Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        common.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, common)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def login(self):
        code, headers, _ = self.request("POST", "/api/login", {"password": TEST_PASSWORD})
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        code, _, body = self.request("GET", "/api/state")
        self.assertEqual(code, 200)
        self.csrf = json.loads(body)["csrf"]

    def test_business_data_and_actions_require_owner_session(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/draft/anything")[0], 401)
        self.assertEqual(self.request("POST", "/api/trigger", {})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "wrong"})[0], 401)
        self.assertFalse(self.service.store.snapshot()["proposals"])

    def test_csrf_origin_host_and_malformed_cookie(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/trigger", {}, {"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/trigger", {}, {"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": "=broken"})[0], 401)
        self.assertFalse(self.service.store.snapshot()["proposals"])

    def test_mobile_state_available_before_first_proposal_and_logout(self):
        self.login()
        code, headers, body = self.request("GET", "/api/state")
        state = json.loads(body)
        self.assertEqual(len(state["ingredients"]), 4)
        self.assertEqual(state["trigger_time"], "22:00")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 401)

    def test_full_http_approval_downloads_correct_edited_drafts(self):
        self.login()
        code, _, body = self.request("POST", "/api/trigger", {})
        self.assertEqual(code, 200)
        proposal = json.loads(body)
        code, _, body = self.request("POST", "/api/approve", {
            "id": proposal["id"], "revision": proposal["revision"], "input_hash": proposal["input_hash"],
            "quantities": {"coffee_beans": 4000, "milk": 0}, "acknowledged": True})
        self.assertEqual(code, 200, body)
        order = json.loads(body)
        self.assertFalse(order["sent"])
        milk = next(i for i in order["items"] if i["ingredient_id"] == "milk")
        self.assertEqual(milk["approved_quantity"], 0)
        self.assertTrue(milk["approval_warnings"])
        drafts = self.service.store.drafts_for(order["id"])
        beans = next(d for d in drafts if d["supplier_id"] == "BEANS_DEMO")
        code, headers, txt = self.request("GET", "/draft/" + beans["id"] + "?format=txt")
        self.assertEqual(code, 200)
        self.assertIn("4000 g", txt.decode())
        code, _, eml = self.request("GET", "/draft/" + beans["id"] + "?format=eml")
        self.assertEqual(code, 200)
        self.assertIn(b"X-Unsent: 1", eml)
        self.assertEqual(len(self.service.store.snapshot()["orders"]), 1)

    def test_concurrent_double_click_creates_one_order(self):
        self.login()
        proposal = json.loads(self.request("POST", "/api/trigger", {})[2])
        payload = {"id": proposal["id"], "revision": proposal["revision"],
                   "input_hash": proposal["input_hash"], "quantities": {}, "acknowledged": True}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.request("POST", "/api/approve", payload)[0], range(2)))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(len(self.service.store.snapshot()["orders"]), 1)


if __name__ == "__main__":
    unittest.main()
