"""Run existing v3.3 integration route tests with in-memory HTTP transport.

This helper does not replace or alter any test case. It substitutes the server
listener and client socket transport only. Real BaseHTTPRequestHandler parsing,
owner login, session state, Host checks, CSRF checks and route dispatch still run.
No socket is opened and service/provider methods remain the existing Mocks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path(__file__).resolve().parents[1] / "local_app")
    args = parser.parse_args()
    app = args.app.resolve()
    sys.path[:0] = [str(app), str(app / "tests")]
    from test_integration_web_v33 import IntegrationWebTests
    from test_catalog_v34 import CatalogHttpWithoutSocketTests

    observed = []
    original_request = IntegrationWebTests.request

    def listener(address, handler):
        return SimpleNamespace(
            RequestHandlerClass=handler, server_port=8765,
            serve_forever=lambda: None, shutdown=lambda: None,
            server_close=lambda: None,
        )

    def request_in_memory(self, method, path, data=None, headers=None):
        status, response_headers, payload = CatalogHttpWithoutSocketTests.request(self, method, path, data, headers)
        decoded = json.loads(payload) if response_headers.get("Content-Type", "").startswith("application/json") else payload.decode("utf-8")
        if method == "GET" and path == "/api/state" and status == 200:
            self.assertIsInstance(decoded["csrf"], str)
            self.assertTrue(decoded["csrf"])
            self.assertIsInstance(decoded["catalog"], dict)
            self.assertIsInstance(decoded["catalog"]["categories"], list)
            self.assertIsInstance(decoded["catalog"]["templates"], list)
            self.assertEqual(decoded["catalog"]["suppliers"], [])
            observed.append({"status": status, "csrf_present": True, "catalog_present": True, "suppliers": []})
        return status, response_headers, decoded

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IntegrationWebTests)
    try:
        with tempfile.TemporaryDirectory(prefix="v34-mock-http-", dir=str(Path(__file__).resolve().parent)) as scratch, \
             patch.object(tempfile, "tempdir", scratch), \
             patch("cafe_web.ThreadingHTTPServer", listener), \
             patch.object(IntegrationWebTests, "request", request_in_memory), \
             patch("socket.socket", side_effect=AssertionError("Verification must not open sockets")), \
             patch("socket.create_connection", side_effect=AssertionError("Verification must not connect to network")):
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        # patch.object restores the test transport even when a test raises.
        assert IntegrationWebTests.request is original_request
    summary = {"transport": "in-memory HTTP streams; no socket or provider calls", "tests_run": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
               "state_200_contract_checks": len(observed), "success": result.wasSuccessful() and len(observed) >= 10}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
