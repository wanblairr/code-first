"""Portable test runner that keeps temporary files inside this project's runtime directory."""
import io
import json
import os
import platform
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

base = Path(__file__).resolve().parent
scratch = base / "runtime" / "test_tmp"
scratch.mkdir(parents=True, exist_ok=True)
assert scratch.resolve().is_relative_to(base.resolve())
tempfile.tempdir = str(scratch)
os.chdir(base)
output = io.StringIO()
suite = unittest.defaultTestLoader.discover(str(base / "tests"))
result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
report_dir = base / "reports"
report_dir.mkdir(exist_ok=True)
(report_dir / "engineering_tests.txt").write_text(output.getvalue(), encoding="utf-8")
summary = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
           "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
           "skipped": len(result.skipped), "success": result.wasSuccessful(),
           "live_llm_tested": False, "model_weights_trained": False,
           "scope": "工程单元/接口替身测试，不代表真实 Llama 输出质量或门店预测准确率"}
(report_dir / "engineering_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
if not result.wasSuccessful():
    print(output.getvalue())
raise SystemExit(0 if result.wasSuccessful() else 1)
