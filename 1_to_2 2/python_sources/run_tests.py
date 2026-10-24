"""Run engineering tests in a checked SQLite-capable temporary directory.

The project may live on a network share. Test databases therefore use the OS
temporary directory by default, not a folder beside this file. Override with
``--temp-dir`` or ``CAFE_TEST_TEMP_DIR`` when the OS temporary path is unsuitable.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import io
import json
import os
import platform
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
TEMP_ENV = "CAFE_TEST_TEMP_DIR"


def select_temp_root(explicit=None):
    """Resolve one selected root; never fall back to the project directory."""
    configured = explicit or os.environ.get(TEMP_ENV)
    selected = Path(configured if configured else tempfile.gettempdir()).expanduser().resolve()
    if not configured and selected.is_relative_to(BASE.resolve()):
        # tempfile.gettempdir() can silently choose the current working directory
        # if all normal candidates fail. That would reintroduce shared-drive DBs.
        raise OSError("系统临时目录退回到了项目目录；请显式选择本地临时目录。")
    selected.mkdir(parents=True, exist_ok=True)
    if not selected.is_dir():
        raise OSError("测试临时路径不是目录。")
    return selected


def probe_sqlite(scratch):
    """Check read/write and two-connection writer locking in our own scratch."""
    database = Path(scratch) / "sqlite-filesystem-probe.sqlite3"
    first = second = None
    try:
        first = sqlite3.connect(database, timeout=.1)
        first.execute("CREATE TABLE check_storage(value INTEGER NOT NULL)")
        first.execute("INSERT INTO check_storage VALUES (42)")
        first.commit()
        second = sqlite3.connect(database, timeout=.1)
        if second.execute("SELECT value FROM check_storage").fetchone() != (42,):
            raise OSError("SQLite 写入后的读取校验失败。")
        first.execute("BEGIN IMMEDIATE")
        try:
            second.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise
        else:
            second.rollback()
            raise OSError("临时目录未提供 SQLite 所需的并发写入锁。")
        first.rollback()
    finally:
        if second is not None:
            second.close()
        if first is not None:
            first.close()


@contextmanager
def checked_temp_directory(root):
    """Limit all tests' temporary files to a unique, disposable child directory."""
    root = Path(root).resolve()
    temporary = tempfile.TemporaryDirectory(prefix="cafe-tests-", dir=str(root))
    scratch = Path(temporary.name).resolve()
    # Verify the exact deletion target before TemporaryDirectory can clean it up.
    if scratch.parent != root or not scratch.name.startswith("cafe-tests-"):
        raise OSError("生成的测试临时目录不在指定根目录内。")
    previous = tempfile.tempdir
    try:
        probe_sqlite(scratch)
        tempfile.tempdir = str(scratch)
        yield scratch
    finally:
        tempfile.tempdir = previous
        if Path(temporary.name).resolve() != scratch or scratch.parent != root:
            raise OSError("临时目录位置已变化，已停止清理。")
        temporary.cleanup()


def run_suite(base, scratch):
    previous_cwd = Path.cwd()
    output = io.StringIO()
    try:
        os.chdir(base)
        suite = unittest.defaultTestLoader.discover(str(base / "tests"))
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    finally:
        os.chdir(previous_cwd)
    report_dir = base / "reports"
    report_dir.mkdir(exist_ok=True)
    (report_dir / "engineering_tests.txt").write_text(output.getvalue(), encoding="utf-8")
    summary = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "success": result.wasSuccessful(),
               "live_llm_tested": False, "llama_weights_trained": False,
               "demand_model_parameters_fitted": (base / "models" / "demand_v3.json").exists(),
               "data_source": "synthetic", "sqlite_temp_verified": True,
               "temporary_root": str(scratch.parent),
               "scope": "工程单元/HTTP集成测试；模拟回测另见训练报告，不代表真实门店准确率"}
    (report_dir / "engineering_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not result.wasSuccessful():
        print(output.getvalue())
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="在支持 SQLite 文件锁的本地临时目录运行测试")
    parser.add_argument("--temp-dir", help=f"测试临时根目录，优先于 {TEMP_ENV}；默认系统临时目录")
    args = parser.parse_args(argv)
    try:
        root = select_temp_root(args.temp_dir)
        with checked_temp_directory(root) as scratch:
            return run_suite(BASE, scratch)
    except (OSError, sqlite3.Error) as error:
        print("测试环境未准备好：" + str(error), file=sys.stderr)
        print("请用 --temp-dir 或 CAFE_TEST_TEMP_DIR 指向有读写权限、支持 SQLite 文件锁的本地磁盘目录。"
              "例如 Windows：python run_tests.py --temp-dir \"%LOCALAPPDATA%\\CafeReplenishment\\test_tmp\"。"
              "不要使用 9p、SMB 或云盘同步目录保存测试数据库。", file=sys.stderr)
        print("应用运行数据库也需本地存储；项目可放共享盘，运行时请为 cafe_app.py 指定本地 --base。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
