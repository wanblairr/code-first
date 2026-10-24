"""Portable test storage, explicit overrides, and actionable filesystem failures."""
import contextlib
import importlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import run_tests as runner


class TestRunnerStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self):
        self.directory.cleanup()

    def test_import_does_not_run_tests_change_cwd_or_override_temp(self):
        cwd, temporary = Path.cwd(), tempfile.tempdir
        with patch.object(unittest.defaultTestLoader, "discover") as discover:
            importlib.reload(runner)
        discover.assert_not_called()
        self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(tempfile.tempdir, temporary)

    def test_default_uses_os_temp_outside_project(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runner, "BASE", self.root / "project"), patch("run_tests.tempfile.gettempdir", return_value=str(self.root)):
            self.assertEqual(runner.select_temp_root(), self.root)

    def test_cli_override_precedes_environment_and_does_not_inspect_default(self):
        with patch.dict(os.environ, {runner.TEMP_ENV: str(self.root / "environment")}), patch("run_tests.tempfile.gettempdir") as default:
            self.assertEqual(runner.select_temp_root(self.root / "cli"), self.root / "cli")
        default.assert_not_called()

    def test_environment_override_is_used_without_silent_fallback(self):
        with patch.dict(os.environ, {runner.TEMP_ENV: str(self.root / "environment")}), patch("run_tests.tempfile.gettempdir") as default:
            self.assertEqual(runner.select_temp_root(), self.root / "environment")
        default.assert_not_called()

    def test_os_temp_fallback_into_project_is_rejected(self):
        project = self.root / "shared-project"
        with patch.dict(os.environ, {}, clear=True), patch.object(runner, "BASE", project), patch("run_tests.tempfile.gettempdir", return_value=str(project / "runtime")):
            with self.assertRaisesRegex(OSError, "项目目录"):
                runner.select_temp_root()
        self.assertFalse(project.exists())

    def test_checked_temp_uses_real_sqlite_locking_and_restores_state(self):
        previous = tempfile.tempdir
        marker = self.root / "keep.txt"
        marker.write_text("user data", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with runner.checked_temp_directory(self.root) as scratch:
                self.assertEqual(scratch.parent, self.root)
                self.assertEqual(Path(tempfile.gettempdir()), scratch)
                self.assertTrue((scratch / "sqlite-filesystem-probe.sqlite3").exists())
                with tempfile.TemporaryDirectory() as nested:
                    self.assertEqual(Path(nested).parent, scratch)
                raise RuntimeError("test failure")
        self.assertEqual(tempfile.tempdir, previous)
        self.assertFalse(scratch.exists())
        self.assertEqual(list(self.root.iterdir()), [marker])

    def test_sqlite_probe_failure_aborts_before_suite_and_keeps_user_data(self):
        marker = self.root / "keep.txt"
        marker.write_text("do not change", encoding="utf-8")
        error = io.StringIO()
        with patch("run_tests.probe_sqlite", side_effect=sqlite3.OperationalError("disk I/O error")), patch("run_tests.run_suite") as suite, contextlib.redirect_stderr(error):
            self.assertEqual(runner.main(["--temp-dir", str(self.root)]), 2)
        suite.assert_not_called()
        self.assertIn("disk I/O error", error.getvalue())
        self.assertIn("--temp-dir", error.getvalue())
        self.assertIn("本地", error.getvalue())
        self.assertEqual(list(self.root.iterdir()), [marker])

    def test_probe_detects_missing_writer_exclusion(self):
        class FakeConnection:
            def execute(self, query):
                return self
            def fetchone(self):
                return (42,)
            def commit(self):
                pass
            def rollback(self):
                pass
            def close(self):
                pass
        with patch("run_tests.sqlite3.connect", side_effect=[FakeConnection(), FakeConnection()]):
            with self.assertRaisesRegex(OSError, "并发写入锁"):
                runner.probe_sqlite(self.root)
