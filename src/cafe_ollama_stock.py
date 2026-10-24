"""Main entry point. No arguments opens a local desktop form."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from cafe_core import AuditStore, CafeError, OllamaClient, json_text, run_forecast, save_json
from cafe_io import demo_context, export_result, read_input, write_template

# 源码在 src/ 下，运行产物（runtime/、reports/）统一放在仓库根目录
BASE = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description="咖啡店 AI 销量预测与模拟补货审批。默认打开图形界面。")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui")
    sub.add_parser("doctor", help="检查 Ollama 服务和模型，不自动下载大模型")
    p = sub.add_parser("predict", help="执行十次模型预测")
    p.add_argument("input", help="JSON 或 Excel 模板")
    p.add_argument("--out", default="runtime/result.json")
    p.add_argument("--calibration", help="可选：已通过留出评估的偏差校准 JSON")
    p = sub.add_parser("template")
    p.add_argument("--out", default="examples/cafe_input.xlsx")
    p = sub.add_parser("evaluate", help="对合成标签做真实模型回测，可能耗时较长")
    p.add_argument("cases")
    p.add_argument("--limit", type=int, default=14)
    p.add_argument("--out", default="runtime/live_evaluation.json")
    p = sub.add_parser("calibrate", help="拟合偏差校准，非 Llama 权重微调")
    p.add_argument("report")
    p.add_argument("--out", default="runtime/calibration.json")
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from cafe_gui import launch
            launch()
        elif args.command == "doctor":
            print(json_text(OllamaClient().health()))
        elif args.command == "template":
            write_template(args.out)
            print(str(Path(args.out).resolve()))
        elif args.command == "predict":
            c, h = read_input(args.input)
            result = run_forecast(c, h, OllamaClient(), progress=lambda s: print(s, flush=True))
            if args.calibration:
                from cafe_eval import apply_calibration
                apply_calibration(result, json.loads(Path(args.calibration).read_text(encoding="utf-8-sig")))
            store = AuditStore(BASE / "runtime" / "cafe.sqlite3")
            store.save_run(result)
            export_result(args.out, result, store.snapshot())
            print(json_text({"status": result["status"], "summary": result.get("summary"), "procurement": result.get("procurement"), "warnings": result["warnings"]}))
            return 0 if result["status"] in ("needs_review", "closed") else 2
        elif args.command == "evaluate":
            from cafe_eval import backtest
            if args.limit < 1:
                raise CafeError("limit 必须为正数。")
            client = OllamaClient()
            client.health()  # Fail early; do not loop through many disconnected requests.
            cases = json.loads(Path(args.cases).read_text(encoding="utf-8-sig"))[:args.limit]
            report = backtest(cases, client, progress=lambda s: print(s, flush=True))
            save_json(args.out, report)
            print(json_text({k: v for k, v in report.items() if k not in ("rows", "failed")}))
        elif args.command == "calibrate":
            from cafe_eval import calibrate
            report = json.loads(Path(args.report).read_text(encoding="utf-8-sig"))
            output = calibrate(report)
            save_json(args.out, output)
            print(json_text(output))
        return 0
    except (CafeError, OSError, ValueError) as error:
        print("操作未完成：" + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
