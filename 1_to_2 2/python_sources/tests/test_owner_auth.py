"""Configured password verification and replacement of legacy runtime secrets."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime

from cafe_app import prepare_demo
from cafe_auth import AUTH_CONFIG, load_auth_policy, make_auth_policy, verify_owner_password
from cafe_core import CafeError, save_json
from cafe_service import CafeService, SHANGHAI
from cafe_web import make_server

PASSWORD = "isolated-auth-fixture-password"


class OwnerPasswordUnitTests(unittest.TestCase):
    def test_configured_password_is_verified_against_salted_hash(self):
        raw = AUTH_CONFIG.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw)["algorithm"], "pbkdf2_sha256")
        self.assertEqual(set(json.loads(raw)), {"algorithm", "iterations", "salt_base64", "digest_hex"})
        load_auth_policy()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.json"
            save_json(path, make_auth_policy(PASSWORD))
            self.assertNotIn(PASSWORD, path.read_text(encoding="utf-8"))
            policy = load_auth_policy(path)
            self.assertTrue(verify_owner_password(PASSWORD, policy))
            self.assertFalse(verify_owner_password(PASSWORD + "x", policy))
            self.assertFalse(verify_owner_password(PASSWORD.upper(), policy))

    def test_invalid_or_unbounded_password_inputs_fail(self):
        for value in (None, True, 123, [], "", "x" * 513, "\ud800"):
            with self.subTest(kind=type(value).__name__):
                self.assertFalse(verify_owner_password(value))

    def test_missing_or_invalid_policy_is_an_explicit_startup_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.json"
            with self.assertRaisesRegex(CafeError, "登录配置"):
                load_auth_policy(path)
            for value in ({}, {"password": PASSWORD}, "not-json"):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(CafeError, "登录配置"):
                    load_auth_policy(path)

    def test_raw_password_is_absent_from_public_assets(self):
        web = AUTH_CONFIG.parent / "web_v3"
        for name in ("index.html", "app.js", "style.css"):
            with self.subTest(name=name):
                self.assertNotIn(PASSWORD, (web / name).read_text(encoding="utf-8"))


class OwnerPasswordHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        prepare_demo(self.base, "2026-10-22")
        self.service = CafeService(self.base, clock=lambda: datetime(2026, 10, 22, 21, tzinfo=SHANGHAI))
        self.old_secret = self.service.runtime / "owner_secret.json"
        save_json(self.old_secret, {"password": "old-generated-runtime-secret"})
        self.old_bytes = self.old_secret.read_bytes()
        auth_config = self.service.runtime / "test_auth.json"
        save_json(auth_config, make_auth_policy(PASSWORD))
        self.server = make_server(self.service, port=0, password_config=auth_config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        common = {"Content-Type": "application/json"}
        common.update(headers or {})
        connection.request(method, path, json.dumps(value) if value is not None else None, common)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_old_runtime_password_rejected_and_new_password_unlocks_existing_runtime(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": "old-generated-runtime-secret"})[0], 401)
        status, headers, body = self.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        status, _, raw = self.request("GET", "/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertNotIn(PASSWORD, raw.decode("utf-8"))
        self.assertNotIn("digest_hex", raw.decode("utf-8"))
        self.assertEqual(self.old_secret.read_bytes(), self.old_bytes)
        self.assertFalse(self.service.store.snapshot()["orders"])

    def test_rate_limit_and_invalid_types_do_not_bypass_verification(self):
        for _ in range(10):
            self.assertEqual(self.request("POST", "/api/login", {"password": None})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": PASSWORD})[0], 429)
        self.assertFalse(self.service.store.snapshot()["orders"])

    def test_fresh_runtime_uses_same_policy_without_creating_plaintext_secret(self):
        self.old_secret.unlink()
        self.assertEqual(self.request("POST", "/api/login", {"password": PASSWORD})[0], 200)
        self.assertFalse(self.old_secret.exists())


if __name__ == "__main__":
    unittest.main()
