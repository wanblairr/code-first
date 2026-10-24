"""Run the Windows launchers against a harmless CLI stub, including failures."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime


BASE = Path(__file__).resolve().parent.parent
STUB = '''import json, os, sys
from pathlib import Path
commands = {"demo", "demo-context", "train", "run", "weather", "serve"}
command = next((arg for arg in sys.argv[1:] if arg in commands), "unknown")
with Path(os.environ["CAFE_STARTUP_TRACE"]).open("a", encoding="utf-8") as f:
    f.write(json.dumps({"command":command, "args":sys.argv[1:]}, ensure_ascii=False)+"\\n")
print("stub " + command, flush=True)
raise SystemExit(json.loads(os.environ["CAFE_STUB_EXIT_CODES"]).get(command, 0))
'''


@unittest.skipUnless(os.name == "nt", "Windows cmd.exe launchers")
class StartupCompatibilityTests(unittest.TestCase):
    def run_launcher(self, name="start_demo_v3.cmd", failures=None, custom_base=False):
        # run_tests.py selects a checked local temp root. No real service or DB
        # is opened by the stub; failures exercise cmd.exe's actual control flow.
        with tempfile.TemporaryDirectory(prefix="cafe-startup-") as temporary:
            folder = Path(temporary) / "启动脚本 with spaces"
            folder.mkdir()
            for launcher in BASE.glob("*.cmd"):
                shutil.copy2(launcher, folder / launcher.name)
            (folder / "cafe_app.py").write_text(STUB, encoding="utf-8")
            trace = folder / "trace.jsonl"
            environment = os.environ.copy()
            environment.pop("CAFE_DEMO_BASE", None)
            environment["CAFE_STARTUP_TRACE"] = str(trace)
            environment["CAFE_STUB_EXIT_CODES"] = json.dumps(failures or {})
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
            expected_base = folder
            if custom_base:
                expected_base = Path(temporary) / "本地数据 & with spaces"
                environment["CAFE_DEMO_BASE"] = str(expected_base)
            result = subprocess.run(
                [environment.get("COMSPEC", "cmd.exe"), "/d", "/c", name],
                cwd=folder, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=20, check=False,
            )
            records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
            return result.returncode, records, expected_base, result.stdout.decode("utf-8", errors="replace")

    def test_all_cmd_files_use_utf8_without_bom_and_only_crlf(self):
        for path in BASE.glob("*.cmd"):
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
                raw.decode("utf-8")
                self.assertTrue(raw.endswith(b"\r\n"))
                self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_failed_suggestion_still_starts_workbench(self):
        code, records, _, output = self.run_launcher(failures={"run": 2})
        self.assertEqual([r["command"] for r in records], ["demo", "run", "serve"], output)
        self.assertEqual(code, 0, output)
        self.assertIn("--lan", records[-1]["args"])

    def test_failed_preparation_stops_with_nonzero_exit(self):
        code, records, _, output = self.run_launcher(failures={"demo": 2})
        self.assertEqual([r["command"] for r in records], ["demo"], output)
        self.assertEqual(code, 2, output)

    def test_failed_server_is_reported_as_failure_after_pause(self):
        code, records, _, output = self.run_launcher(failures={"serve": 2})
        self.assertEqual([r["command"] for r in records], ["demo", "run", "serve"], output)
        self.assertEqual(code, 2, output)

    def test_runtime_override_with_spaces_and_unicode_reaches_every_command(self):
        code, records, expected, output = self.run_launcher(custom_base=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(records), 3, output)
        for record in records:
            self.assertIn("--base", record["args"])
            self.assertEqual(record["args"][record["args"].index("--base") + 1], str(expected))

    if (BASE / "start_weather_demo.cmd").exists():
        def test_weather_training_or_fetch_failure_still_starts_workbench(self):
            for command in ("train", "weather", "run"):
                with self.subTest(command=command):
                    code, records, _, output = self.run_launcher("start_weather_demo.cmd", failures={command: 2}, custom_base=True)
                    self.assertEqual([r["command"] for r in records], ["demo-context", "train", "weather", "run", "serve"], output)
                    self.assertEqual(code, 0, output)

        def test_default_start_opens_weather_workbench_with_initial_proposal(self):
            code, records, _, output = self.run_launcher("start.cmd", custom_base=True)
            self.assertEqual([r["command"] for r in records], ["demo-context", "train", "weather", "run", "serve"], output)
            self.assertEqual(code, 0, output)
            self.assertEqual(records[-1]["args"][-2:], ["--port", "8768"])


class DemoSnapshotRetentionTests(unittest.TestCase):
    def test_crossday_reuse_preserves_inputs_and_blocks_new_approval(self):
        from cafe_app import prepare_demo
        from cafe_core import CafeError
        from cafe_service import CafeService, SHANGHAI
        with tempfile.TemporaryDirectory(prefix="cafe-stale-") as temporary:
            base = Path(temporary)
            data = prepare_demo(base, "2026-10-22")
            before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
            instant = [datetime(2026, 10, 22, 21, tzinfo=SHANGHAI)]
            service = CafeService(base, clock=lambda: instant[0])
            proposal = service.trigger()
            service.store.log("retention_marker", {"preserve": True})
            prior_store = service.store.snapshot()
            instant[0] = datetime(2026, 10, 23, 12, tzinfo=SHANGHAI)
            self.assertEqual(prepare_demo(base, "2026-10-23"), data)
            self.assertEqual({p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}, before)
            readiness = service.readiness()
            self.assertEqual(readiness["status"], "needs_update")
            self.assertEqual(readiness["inventory"]["status"], "stale")
            self.assertEqual(readiness["sales"]["status"], "stale")
            with self.assertRaisesRegex(CafeError, "库存不是今天"):
                service.trigger()
            with self.assertRaisesRegex(CafeError, "跨日"):
                service.approve(proposal["id"], proposal["revision"], proposal["input_hash"], {}, True)
            self.assertEqual(service.store.snapshot(), prior_store)

    if (BASE / "cafe_context.py").exists():
        def test_context_restart_preserves_existing_weather_snapshot_and_hash(self):
            from cafe_app import prepare_context_demo
            from cafe_core import fingerprint
            from cafe_service import CafeService, SHANGHAI
            with tempfile.TemporaryDirectory(prefix="cafe-context-retain-") as temporary:
                base = Path(temporary)
                data = prepare_context_demo(base, "2026-10-22")
                service = CafeService(base, clock=lambda: datetime(2026, 10, 22, 21, tzinfo=SHANGHAI))
                before_bytes = (data / "target_context.json").read_bytes()
                before_hash = fingerprint(service.inputs())
                self.assertEqual(prepare_context_demo(base, "2026-10-23"), data)
                self.assertEqual((data / "target_context.json").read_bytes(), before_bytes)
                self.assertEqual(fingerprint(service.inputs()), before_hash)


if __name__ == "__main__":
    unittest.main()
