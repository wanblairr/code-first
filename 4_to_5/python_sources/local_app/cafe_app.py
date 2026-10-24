"""v3 entry point: synthetic training and the complete review-to-draft workflow."""
from __future__ import annotations
import argparse
import json
import socket
import sqlite3
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

from cafe_core import CafeError, json_text, save_json
from cafe_service import CafeService, load_json, local_now

BASE = Path(__file__).resolve().parent


def prepare_demo(base, as_of=None, reset=False):
    from cafe_forecast import make_demo_sales
    base = Path(base)
    target_dir = base / "runtime_v3" / "demo_data"
    if (target_dir / "sales.csv").exists() and not reset:
        return target_dir
    today = date.fromisoformat(as_of or local_now().date().isoformat())
    target_dir.mkdir(parents=True, exist_ok=True)
    delta = today - date(2026, 10, 22)
    for filename in ("inventory.json", "recipes.json", "suppliers.json"):
        data = load_json(BASE / "examples_v3" / filename)
        if filename == "inventory.json":
            data["as_of_date"] = today.isoformat()
            data["snapshot_id"] = f"demo-closing-{today.isoformat()}"
            for item in data["items"]:
                for batch in item["batches"]:
                    batch["usable_through"] = (date.fromisoformat(batch["usable_through"]) + delta).isoformat()
                for inbound in item["inbounds"]:
                    inbound["arrival_date"] = (date.fromisoformat(inbound["arrival_date"]) + delta).isoformat()
        if filename == "suppliers.json":
            for supplier in data["suppliers"]:
                supplier["delivery_date"] = (today + timedelta(days=1)).isoformat()
        save_json(target_dir / filename, data)
    make_demo_sales(target_dir / "sales.csv", today.isoformat())
    return target_dir


def prepare_context_demo(base, as_of=None, reset=False):
    from cafe_forecast_context import make_demo_context_data
    target_dir = prepare_demo(base, as_of, reset)
    if not reset and (target_dir / "environment.json").exists() and (target_dir / "target_context.json").exists():
        # Restarting must not silently change an existing approval input hash.
        return target_dir
    if reset or not (target_dir / "environment.json").exists():
        _, _, context = make_demo_context_data(target_dir, end_date=as_of or local_now().date().isoformat())
        save_json(target_dir / "target_context.json", context)
    # This snapshot is simulated. Never relabel it as live weather.
    context = load_json(target_dir / "target_context.json")
    if context["date"] == (local_now().date() + timedelta(days=1)).isoformat():
        context["forecast_origin"] = local_now().isoformat()
    save_json(target_dir / "target_context.json", context)
    return target_dir


def export_order(service, order_id, out):
    folder = Path(out) / order_id
    folder.mkdir(parents=True, exist_ok=True)
    drafts = service.store.drafts_for(order_id)
    if not drafts:
        raise CafeError("没有该订单的草稿。")
    for draft in drafts:
        # IDs are generated internally; no supplier-supplied path components.
        (folder / (draft["id"] + ".eml")).write_bytes(draft["email_eml"].encode("utf-8"))
        (folder / (draft["id"] + ".txt")).write_text(draft["body"], encoding="utf-8")
    return str(folder.resolve())


def main(argv=None):
    parser = argparse.ArgumentParser(description="咖啡店补货 v3.7：模拟模型训练、22点定时、手机审批与采购草稿")
    parser.add_argument("--base", default=str(BASE), help="独立运行记录目录，默认项目目录；共享盘源码请改用本地磁盘目录")
    parser.add_argument("--data-dir", help="模拟 CSV/JSON 数据目录")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo", help="创建今天的模拟数据；默认保留已有数据")
    p.add_argument("--date", help="复现用模拟日期，默认上海今天")
    p.add_argument("--reset", action="store_true", help="重建模拟输入，保留审批数据库")
    p = sub.add_parser("demo-context", help="创建吉隆坡模拟天气、销量和已核验节假日资料")
    p.add_argument("--date")
    p.add_argument("--reset", action="store_true")
    sub.add_parser("bom-run", help="按CSV配方和服务器库存生成BOM补货建议，不会审批或外发")
    sub.add_parser("bom-state", help="查看持久化CSV库存及安全线状态")
    sub.add_parser("weather", help="从 Open-Meteo 刷新明日吉隆坡天气；失败保留旧快照")
    sub.add_parser("train", help="拟合需求模型并保存时间回测报告")
    sub.add_parser("run", help="立即生成明天的采购建议，不会批准或发送")
    p = sub.add_parser("serve", help="启动手机审批网页和每天22点触发器")
    p.add_argument("--lan", action="store_true", help="监听局域网供同Wi-Fi手机演示，HTTP仅限模拟数据")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("export", help="导出指定已批准订单的邮件/WhatsApp文本草稿")
    p.add_argument("order_id")
    p.add_argument("--out", default="runtime_v3/drafts")
    args = parser.parse_args(argv)
    try:
        base = Path(args.base).resolve()
        if args.command in ("demo", "demo-context"):
            data_dir = (prepare_context_demo if args.command == "demo-context" else prepare_demo)(base, args.date, args.reset)
            print("模拟数据已准备：" + str(data_dir.resolve()))
            print("来源：合成销量、3类饮品；不包含真实门店记录。")
            expected = args.date or local_now().date().isoformat()
            actual = load_json(data_dir / "inventory.json")["as_of_date"]
            if actual != expected:
                print(f"已保留既有输入：库存日期 {actual}，本次日期 {expected}。数据需要更新，未自动改写销量或盘点；网页仍可启动查看状态。")
            return 0
        service = CafeService(base, args.data_dir)
        if args.command == "train":
            model = service.train()
            report = {k: model[k] for k in ("version", "training_data_sha256", "artifact_sha256", "notes", "evaluation") if k in model}
            save_json(service.runtime / "training_report.json", report)
            print(json_text({"model": str(service.model_path), "report": str(service.runtime / "training_report.json"), "data_source": "synthetic"}))
        elif args.command == "run":
            proposal = service.trigger()
            save_json(service.runtime / "latest_proposal.json", proposal)
            print(json_text({"proposal_id": proposal["id"], "target_date": proposal["target_date"],
                             "items": proposal["plan"]["items"], "blockers": proposal["plan"]["blockers"]}))
        elif args.command == "weather":
            print(json_text(service.refresh_weather()))
        elif args.command == "export":
            print(export_order(service, args.order_id, args.out))
        elif args.command in ("bom-run", "bom-state"):
            service.bom_enabled = True
            state = service.bom().suggest() if args.command == "bom-run" else service.bom_state()
            if args.command == "bom-run":
                proposal = state["proposal"]
                print(json_text({"proposal_id": proposal["id"], "status": proposal["status"],
                    "target_date": proposal["target_date"], "stock_revision": state["revision"],
                    "items": [row for row in proposal["rows"] if row["quantity"] > 0],
                    "data_source": "owner_entered" if proposal["forecast"]["source"] == "owner_sales_recency_weighted_v1" else "synthetic",
                    "forecast_source": proposal["forecast"]["source"], "sent": False}))
            else:
                print(json_text(state))
        elif args.command == "serve":
            service.bom_enabled = True
            if not 1 <= args.port <= 65535:
                raise CafeError("端口须为1–65535。")
            from cafe_web import make_server
            server = make_server(service, "0.0.0.0" if args.lan else "127.0.0.1", args.port)
            print(f"本机审批：http://127.0.0.1:{server.server_port}")
            if args.lan:
                try:
                    ips = sorted({i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not i[4][0].startswith("127.")})
                except OSError:
                    ips = []
                for ip in ips:
                    print(f"手机同Wi-Fi访问：http://{ip}:{server.server_port}")
                print(f"如上方未列出 Wi-Fi 地址，请用 ipconfig 查看无线网卡 IPv4，手机访问 http://该IPv4:{server.server_port}/。")
                print("局域网HTTP演示仅使用模拟数据；手机网页保持打开才会显示新建议提醒。")
            print("登录页需要已配置的门店访问密码；密码不会在终端或网页源码中显示。")
            print("每天22:00（Asia/Kuala_Lumpur）自动检查。保持此窗口运行，Ctrl+C停止。", flush=True)
            thread = threading.Thread(target=service.scheduler_loop, daemon=True)
            thread.start()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                service.stop.set()
                server.server_close()
                thread.join(timeout=2)
        return 0
    except sqlite3.Error as error:
        print("SQLite 运行数据库无法读写：" + str(error) + "。请将 --base 指向支持文件锁的本地磁盘目录；若已有记录，先备份并迁移原 runtime_v3，勿直接重建采购记录。", file=sys.stderr)
        return 2
    except (CafeError, OSError, ValueError, KeyError) as error:
        print("操作未完成：" + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
