"""NIC-independent Host checks, with real HTTP origin/duplicate-header coverage."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cafe_auth import make_auth_policy
from cafe_core import save_json
from cafe_web import host_is_allowed, make_server

TEST_PASSWORD = "lan-fixture-test-password"


class LanHostValidationTests(unittest.TestCase):
    def check_host(self, authority, local_ip="10.72.182.65", lan=True, port=8765):
        return host_is_allowed(authority, local_ip, port, {"localhost", "cafe-pc"}, lan)

    def test_unlisted_nic_is_authorized_by_actual_connection_destination(self):
        # No hostname resolution or hard-coded Wi-Fi/VPN subnet is involved.
        self.assertTrue(self.check_host("10.72.182.65:8765"))
        self.assertTrue(self.check_host("192.168.50.19:8765", local_ip="192.168.50.19"))
        self.assertFalse(self.check_host("10.72.182.65:8765", lan=False))
        self.assertFalse(self.check_host("10.72.182.66:8765"))
        self.assertFalse(self.check_host("192.168.50.19:8765"))
        self.assertFalse(self.check_host("203.0.113.10:8765"))
        self.assertFalse(self.check_host("0.0.0.0:8765", local_ip="0.0.0.0"))

    def test_explicit_local_names_and_loopbacks_keep_working(self):
        for authority in ("localhost:8765", "CAFE-PC:8765", "127.0.0.1:8765", "[::1]:8765"):
            with self.subTest(authority=authority):
                self.assertTrue(self.check_host(authority, lan=False))
        self.assertTrue(self.check_host("localhost", port=80))

    def test_other_domains_ports_and_ambiguous_authorities_are_rejected(self):
        for authority in (None, "", "attacker.invalid:8765", "cafe-pc.attacker.invalid:8765",
                          "localhost:8766", "localhost", "localhost:bad", "localhost:99999",
                          "attacker@localhost:8765", "localhost:8765/path", "localhost:8765?x=1",
                          "localhost:8765#fragment", "localhost:8765\\foo", "localhost:8765 ",
                          "localhost:8765\t", "[::1", "127.1:8765", "2130706433:8765"):
            with self.subTest(authority=authority):
                self.assertFalse(self.check_host(authority))


class LanHostHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = SimpleNamespace(runtime=Path(self.temp.name), store=Mock())
        password_config = self.service.runtime / "test_auth.json"
        save_json(password_config, make_auth_policy(TEST_PASSWORD))
        # In particular, a missing hostname mapping cannot prevent LAN startup.
        with patch("cafe_web.socket.getaddrinfo", side_effect=OSError("hostname has no NIC mapping")):
            self.server = make_server(self.service, host="0.0.0.0", port=0, password_config=password_config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, host, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Host": host, "Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps({"password": TEST_PASSWORD}) if method == "POST" else None
        connection.request(method, "/api/login" if method == "POST" else "/", body, headers)
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    def test_lan_server_starts_without_hostname_ip_enumeration(self):
        self.assertEqual(self.request("GET", f"127.0.0.1:{self.server.server_port}"), 200)
        self.assertEqual(self.request("GET", f"localhost:{self.server.server_port}"), 200)
        self.assertEqual(self.request("GET", f"10.72.182.65:{self.server.server_port}"), 403)
        self.assertEqual(self.request("GET", f"attacker.invalid:{self.server.server_port}"), 403)
        self.assertEqual(self.request("GET", "127.0.0.1:1"), 403)

    def test_origin_protection_still_applies_to_an_allowed_local_host(self):
        host = f"127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request("POST", host, "https://attacker.invalid"), 403)
        self.assertEqual(self.request("POST", host, "http://" + host), 200)

    def test_duplicate_host_headers_are_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("GET", "/", skip_host=True)
        connection.putheader("Host", f"localhost:{self.server.server_port}")
        connection.putheader("Host", f"127.0.0.1:{self.server.server_port}")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
