"""Synthetic cafe workflow: durable proposals, owner approval and supplier drafts.

No email, WhatsApp or purchasing API is called. A database transaction records
approval and draft bodies together. Inventory changes only through input snapshots.
"""
from __future__ import annotations

import csv
import json
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from urllib.parse import quote

from cafe_core import CafeError, canonical, fingerprint, now, save_json

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def local_now():
    return datetime.now(SHANGHAI)


def load_json(path):
    path = Path(path)
    if path.stat().st_size > 10_000_000:
        raise CafeError("输入超过 10 MB。")
    return json.loads(path.read_text(encoding="utf-8-sig"))


class WorkflowStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
              id TEXT PRIMARY KEY, input_hash TEXT UNIQUE NOT NULL,
              shop_id TEXT NOT NULL, target_date TEXT NOT NULL,
              document TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS orders (
              id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL,
              shop_id TEXT NOT NULL, target_date TEXT NOT NULL, document TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_order_per_day ON orders(shop_id,target_date);
            CREATE TABLE IF NOT EXISTS drafts (
              id TEXT PRIMARY KEY, order_id TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
              actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule_days (day TEXT PRIMARY KEY, proposal_id TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, actor, action, subject, detail):
        db.execute("INSERT INTO events(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
                   (now(), actor, action, subject, canonical(detail)))

    def log(self, action, detail, subject="workflow", actor="system"):
        with self.connect() as db:
            self.event(db, actor, action, subject, detail)

    def proposal(self, proposal_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                raise CafeError("采购建议不存在。")
            return dict(json.loads(row["document"]), status=row["status"], revision=row["revision"])

    def save_proposal(self, document):
        with self.connect() as db:
            old = db.execute("SELECT id FROM proposals WHERE input_hash=?", (document["input_hash"],)).fetchone()
            if old:
                return old[0]
            # New data invalidates pending approval links from older inventory snapshots.
            db.execute("UPDATE proposals SET status='superseded',revision=revision+1 WHERE shop_id=? AND target_date=? AND status='pending'",
                       (document["shop_id"], document["target_date"]))
            db.execute("INSERT INTO proposals(id,input_hash,shop_id,target_date,document,status) VALUES(?,?,?,?,?,?)",
                       (document["id"], document["input_hash"], document["shop_id"], document["target_date"], canonical(document), "pending"))
            self.event(db, "system", "approval_requested", document["id"],
                       {"trigger": document["trigger"], "blockers": document["plan"]["blockers"],
                        "snapshot_id": document["plan"]["snapshot_id"], "input_hash": document["input_hash"]})
            return document["id"]

    def snapshot(self):
        with self.connect() as db:
            proposals = [dict(json.loads(r["document"]), status=r["status"], revision=r["revision"])
                         for r in db.execute("SELECT * FROM proposals ORDER BY rowid DESC LIMIT 50")]
            orders = [json.loads(r[0]) for r in db.execute("SELECT document FROM orders ORDER BY rowid DESC LIMIT 50")]
            events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]
            return {"proposals": proposals, "orders": orders, "events": events}

    def approve(self, proposal_id, revision, input_hash, current_hash, quantities, suppliers, acknowledged=False):
        from cafe_materials import validate_quantities
        if not acknowledged:
            raise CafeError("请先确认已核对单位、数量、交期和预测局限。")
        if type(revision) is not int:
            raise CafeError("审批版本号无效。")
        with self.connect() as db:
            row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise CafeError("建议已处理或已被新数据替代，请刷新页面。")
            doc = json.loads(row["document"])
            if revision != row["revision"] or input_hash != row["input_hash"] or current_hash != row["input_hash"]:
                raise CafeError("库存、销售、配方或模型已更新；必须重新生成建议后审批。")
            items = validate_quantities(doc["plan"], quantities)
            # approved_quantity is separately validated; retain the original suggestion.
            positive = [i for i in items if i["approved_quantity"] > 0]
            if not positive:
                raise CafeError("采购数量全部为零；可使用拒绝按钮，无需生成零数量采购单。")
            order = {"id": str(uuid.uuid4()), "proposal_id": proposal_id,
                     "shop_id": doc["shop_id"], "target_date": doc["target_date"],
                     "approved_at": now(), "approved_by": "owner", "status": "draft",
                     "items": items, "original_items": doc["plan"]["items"],
                     "input_hash": input_hash, "inventory_changed": False, "sent": False,
                     "forecast": doc["forecast"], "analysis": doc.get("analysis")}
            drafts = build_drafts(order, suppliers)
            try:
                db.execute("INSERT INTO orders VALUES(?,?,?,?,?)", (order["id"], proposal_id, order["shop_id"], order["target_date"], canonical(order)))
            except sqlite3.IntegrityError:
                raise CafeError("同店同日已存在采购草稿；已阻止重复采购。请先人工核对原单。") from None
            for draft in drafts:
                db.execute("INSERT INTO drafts VALUES(?,?,?)", (draft["id"], order["id"], canonical(draft)))
            db.execute("UPDATE proposals SET status='approved',revision=revision+1 WHERE id=?", (proposal_id,))
            self.event(db, "owner", "approved_draft_created", proposal_id,
                       {"order_id": order["id"], "items": items, "draft_ids": [d["id"] for d in drafts],
                        "sent": False, "inventory_changed": False})
            return order

    def reject(self, proposal_id, revision, reason=""):
        if not isinstance(reason, str) or len(reason) > 300 or "\x00" in reason:
            raise CafeError("暂不采购的原因须为最多300字的文本。")
        with self.connect() as db:
            changed = db.execute("UPDATE proposals SET status='rejected',revision=revision+1 WHERE id=? AND status='pending' AND revision=?", (proposal_id, revision)).rowcount
            if changed != 1:
                raise CafeError("建议已处理或版本过期，请刷新。")
            self.event(db, "owner", "proposal_rejected", proposal_id, {"reason": reason.strip()})

    def drafts_for(self, order_id):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT document FROM drafts WHERE order_id=? ORDER BY id", (order_id,))]

    def draft(self, draft_id):
        with self.connect() as db:
            r = db.execute("SELECT document FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not r:
                raise CafeError("采购草稿不存在。")
            return json.loads(r[0])


def build_drafts(order, suppliers):
    contacts = {s["supplier_id"]: s for s in suppliers["suppliers"]}
    groups = {}
    for item in order["items"]:
        if item["approved_quantity"] > 0:
            groups.setdefault(item["supplier_id"], []).append(item)
    drafts = []
    for supplier_id, items in sorted(groups.items()):
        contact = contacts[supplier_id]
        subject = f"[模拟采购草稿] {order['shop_id']} / {order['target_date']} / {order['id'][:8]}"
        lines = ["您好，请确认以下采购需求及到货时间：", ""]
        lines += [f"- {i['name']}：{i['approved_quantity']:g} {i['unit']}（包装 {i['pack_size']:g} {i['unit']}）" for i in items]
        lines += ["", f"要求到货日：{order['target_date']} 开店前", f"订单编号：{order['id']}", "请回复价格、可供数量与交期。", "本内容为比赛模拟草稿，尚未发送、付款或入库。"]
        body = "\n".join(lines)
        msg = EmailMessage(policy=SMTP)
        msg["Subject"] = subject
        # X-Unsent prompts compatible mail clients to open the .eml as an editable draft.
        msg["X-Unsent"] = "1"
        if contact.get("email"):
            msg["To"] = contact["email"]
        msg.set_content(body)
        phone = str(contact.get("whatsapp", "")).strip()
        if phone and (not phone.lstrip("+").isdigit() or not 7 <= len(phone.lstrip("+")) <= 15):
            raise CafeError("供应商 WhatsApp 号码须为国际区号与数字。")
        # No fabricated contact numbers in the shipped synthetic examples.
        url = "https://wa.me/" + phone.lstrip("+") + "?text=" + quote(body) if phone else None
        drafts.append({"id": str(uuid.uuid4()), "order_id": order["id"], "supplier_id": supplier_id,
                       "supplier_name": contact["name"], "subject": subject, "body": body,
                       "email": contact.get("email", ""), "email_eml": msg.as_string(),
                       "whatsapp_url": url, "sent": False})
    return drafts


class CafeService:
    def __init__(self, base, data_dir=None, clock=local_now):
        self.base = Path(base).resolve()
        self.runtime = self.base / "runtime_v3"
        self.runtime.mkdir(parents=True, exist_ok=True)
        default_data = self.runtime / "demo_data"
        self.data_dir = Path(data_dir).resolve() if data_dir else (default_data if default_data.exists() else self.base / "examples_v3")
        self.store = WorkflowStore(self.runtime / "cafe.sqlite3")
        self.clock = clock
        self.lock = threading.RLock()
        self.model_path = self.runtime / "demand_model.json"
        self.stop = threading.Event()
        self.last_schedule_failure = None

    def inputs(self):
        from cafe_forecast import read_sales_csv
        with self.lock:
            self._recover_stockout()
            result = {"sales": read_sales_csv(self.data_dir / "sales.csv"),
                    "inventory": load_json(self.data_dir / "inventory.json"),
                    "recipes": load_json(self.data_dir / "recipes.json"),
                    "suppliers": load_json(self.data_dir / "suppliers.json")}
            paths = [self.data_dir / name for name in ("environment.json", "target_context.json")]
            if any(p.exists() for p in paths):
                if not all(p.exists() for p in paths):
                    raise CafeError("天气历史与明日天气日历须同时提供，不能静默忽略缺失资料。")
                result.update(environment=load_json(paths[0]), target_context=load_json(paths[1]))
            return result

    def _recover_stockout(self):
        # A write-ahead journal closes the crash window between the two input files.
        # Readers may use neither half of a stockout update until recovery succeeds.
        journal = self.runtime / "pending_stockout.json"
        if not journal.exists():
            return
        pending = load_json(journal)
        if pending["data_dir"] != str(self.data_dir):
            raise CafeError("存在另一数据目录的未完成用完事件，请使用原数据目录恢复。")
        save_json(self.data_dir / "inventory.json", pending["inventory"])
        sales_path = self.data_dir / "sales.csv"
        temp = sales_path.with_suffix(".csv.tmp")
        with temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(pending["sales"][0]))
            writer.writeheader()
            writer.writerows(pending["sales"])
        temp.replace(sales_path)
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM events WHERE action='stockout_recorded' AND subject=?", (pending["event_id"],)).fetchone():
                self.store.event(db, "owner", "stockout_recorded", pending["event_id"], pending["detail"])
        journal.unlink()

    def source_hash(self, inputs):
        # Include fitted weights, not merely a version name, in approval evidence.
        return fingerprint(dict(inputs, model=load_json(self.model_path)))

    def readiness(self, inputs=None):
        """Report source dates truthfully; reading a file is not a POS sync."""
        inputs = inputs if inputs is not None else self.inputs()
        day = self.clock().astimezone(SHANGHAI).date()
        today = day.isoformat()
        inventory = inputs["inventory"]
        latest = {}
        for row in inputs["sales"]:
            latest[row["sku"]] = max(latest.get(row["sku"], ""), row["date"])
        # A missing recipe SKU is missing data, not zero sales.
        for recipe in inputs["recipes"]["recipes"]:
            latest.setdefault(recipe["sku"], None)
        inventory_fresh = inventory["as_of_date"] == today
        sales_fresh = bool(latest) and all(value == today for value in latest.values())
        return {"today": today, "target_date": (day + timedelta(days=1)).isoformat(),
                "inventory": {"as_of_date": inventory["as_of_date"],
                              "snapshot_id": inventory["snapshot_id"],
                              "status": "fresh" if inventory_fresh else "stale"},
                "sales": {"latest_date_by_sku": latest,
                          "status": "fresh" if sales_fresh else "stale",
                          "completion_verified": False,
                          "note": "仅核对销售记录日期，未验证已收盘、当日销量完整或POS同步完成。"},
                "status": "ready" if inventory_fresh and sales_fresh else "needs_update",
                "data_source": "synthetic",
                "stockout_note": "食材用完会把库存记为0并标记受影响销量售罄；此模拟预测需使用当天完整销量，营业中不完整销量不能当作收盘数据。"}

    def train(self):
        from cafe_forecast import train
        with self.lock:
            inputs = self.inputs()
            if "environment" in inputs:
                from cafe_forecast_context import train as context_train
                result = context_train(inputs["sales"], inputs["environment"], self.model_path)
            else:
                result = train(inputs["sales"], self.model_path)
            save_json(self.runtime / "training_source.json", self.training_stamp(inputs))
            self.store.log("model_trained", {"sales_hash": fingerprint(inputs["sales"]), "artifact_hash": fingerprint(result), "data_source": "synthetic"})
            return result

    def training_stamp(self, inputs):
        result = {"sales_hash": fingerprint(inputs["sales"])}
        if "environment" in inputs:
            from cafe_forecast_context import VERSION
            result.update(environment_hash=fingerprint(inputs["environment"]), model_version=VERSION)
        return result

    def context_state(self, inputs=None):
        from cafe_context_workflow import context_state
        return context_state(self, inputs if inputs is not None else self.inputs())

    def refresh_weather(self):
        from cafe_context_workflow import refresh_weather
        with self.lock:
            return refresh_weather(self)

    def scenario(self, values):
        from cafe_context_workflow import scenario
        with self.lock:
            return scenario(self, values)

    def trigger(self, reason="manual"):
        from cafe_forecast import predict, train
        from cafe_materials import plan
        if reason not in ("manual", "schedule", "stockout"):
            raise CafeError("未知触发类型。")
        with self.lock:
            inputs = self.inputs()
            day = self.clock().astimezone(SHANGHAI).date()
            if inputs["inventory"]["as_of_date"] != day.isoformat():
                raise CafeError(f"库存不是今天 {day} 的快照，请先更新当天销售和库存。")
            # Forecast reads today's sales, unlike the legacy v2 yesterday-only input.
            target = (day + timedelta(days=1)).isoformat()
            stamp = self.runtime / "training_source.json"
            if "target_context" in inputs:
                from cafe_context_workflow import valid_context
                valid_context(self, inputs["target_context"], target)
            if not self.model_path.exists() or not stamp.exists() or load_json(stamp) != self.training_stamp(inputs):
                model = self.train()
            else:
                model = load_json(self.model_path)
            if "target_context" in inputs:
                from cafe_forecast_context import predict as context_predict
                forecast = context_predict(model, inputs["sales"], inputs["target_context"])
            else:
                forecast = predict(model, inputs["sales"], target)
            procurement = plan(forecast, inputs["inventory"], inputs["recipes"], inputs["suppliers"], target)
            doc = {"id": str(uuid.uuid4()), "created_at": now(), "shop_id": procurement["shop_id"],
                   "target_date": target, "trigger": reason, "input_hash": self.source_hash(inputs),
                   "forecast": forecast, "plan": procurement, "data_source": "synthetic"}
            if "target_context" in inputs:
                from cafe_context_workflow import analysis
                doc["analysis"] = analysis(inputs, forecast, procurement)
            proposal_id = self.store.save_proposal(doc)
            return self.store.proposal(proposal_id)

    def approve(self, proposal_id, revision, input_hash, quantities, acknowledged=False):
        with self.lock:
            inputs = self.inputs()
            doc = self.store.proposal(proposal_id)
            if "target_context" in inputs:
                from cafe_context_workflow import valid_context
                valid_context(self, inputs["target_context"], doc["target_date"])
            if doc["plan"]["as_of_date"] != self.clock().astimezone(SHANGHAI).date().isoformat():
                raise CafeError("采购建议已跨日过期，请更新库存后重新生成。")
            return self.store.approve(proposal_id, revision, input_hash, self.source_hash(inputs), quantities, inputs["suppliers"], acknowledged)

    def preview(self, proposal_id, revision, input_hash, quantities):
        """Validate owner edits before confirmation without creating an order."""
        from cafe_materials import validate_quantities
        with self.lock:
            if type(revision) is not int:
                raise CafeError("审批版本号无效。")
            inputs = self.inputs()
            doc = self.store.proposal(proposal_id)
            if "target_context" in inputs:
                from cafe_context_workflow import valid_context
                valid_context(self, inputs["target_context"], doc["target_date"])
            if doc["status"] != "pending":
                raise CafeError("这份建议已经处理，请刷新后查看最新记录。")
            if doc["plan"]["as_of_date"] != self.clock().astimezone(SHANGHAI).date().isoformat():
                raise CafeError("这份建议已跨日，请先更新今日库存，再重新生成。")
            if revision != doc["revision"] or input_hash != doc["input_hash"] or self.source_hash(inputs) != doc["input_hash"]:
                raise CafeError("库存、销售、配方或模型已有变化，请重新生成建议后核对。")
            with self.store.connect() as db:
                existing = db.execute("SELECT id FROM orders WHERE shop_id=? AND target_date=?", (doc["shop_id"], doc["target_date"])).fetchone()
            if existing:
                raise CafeError("同一天已经生成过采购草稿，请先核对已有草稿，避免重复采购。")
            items = validate_quantities(doc["plan"], quantities)
            changes = [{key: item[key] for key in ("ingredient_id", "name", "unit", "suggested_quantity", "approved_quantity")}
                       for item in items if item["approved_quantity"] != item["suggested_quantity"]]
            return {"id": proposal_id, "revision": revision, "input_hash": input_hash,
                    "target_date": doc["target_date"], "items": items, "changes": changes,
                    "warnings": [f"{item['name']}：{warning}" for item in items for warning in item["approval_warnings"]],
                    "supplier_count": len({item["supplier_id"] for item in items if item["approved_quantity"] > 0})}

    def stockout(self, ingredient_id):
        """Persist an explicit physical zero-stock event, not inferred consumption."""
        with self.lock:
            inputs = self.inputs()
            inventory = inputs["inventory"]
            day = self.clock().astimezone(SHANGHAI).date().isoformat()
            if inventory["as_of_date"] != day:
                raise CafeError("请先更新今天的库存快照，再报告食材用完。")
            matches = [i for i in inventory["items"] if i["ingredient_id"] == ingredient_id]
            if not matches:
                raise CafeError("食材不存在。")
            item = matches[0]
            changed = any(b["quantity"] != 0 for b in item["batches"])
            affected = {r["sku"] for r in inputs["recipes"]["recipes"] if any(i["ingredient_id"] == ingredient_id for i in r["ingredients"])}
            for row in inputs["sales"]:
                if row["date"] == day and row["sku"] in affected and not row["stockout"]:
                    row["stockout"] = True
                    changed = True
            if changed:
                for batch in item["batches"]:
                    batch["quantity"] = 0
                inventory["snapshot_id"] = str(uuid.uuid4())
                save_json(self.runtime / "pending_stockout.json",
                          {"event_id": str(uuid.uuid4()), "data_dir": str(self.data_dir),
                           "inventory": inventory, "sales": inputs["sales"],
                           "detail": {"ingredient_id": ingredient_id, "snapshot_id": inventory["snapshot_id"], "censored_skus": sorted(affected)}})
                self._recover_stockout()
            return self.trigger("stockout")

    def scheduler_tick(self, instant=None):
        """22:00 Asia/Kuala_Lumpur (UTC+8); restart catches up once that day."""
        instant = (instant or self.clock()).astimezone(SHANGHAI)
        if instant.hour < 22:
            return None
        day = instant.date().isoformat()
        with self.lock:
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM schedule_days WHERE day=?", (day,)).fetchone():
                    return None
            if "target_context" in self.inputs():
                # Retrieve tomorrow's forecast only at the explicit nightly job,
                # never during page polling. A failed fetch leaves this job retryable.
                self.refresh_weather()
            result = self.trigger("schedule")
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO schedule_days VALUES(?,?)", (day, result["id"]))
                self.store.event(db, "system", "scheduled_trigger_completed", result["id"], {"day": day, "timezone": "Asia/Kuala_Lumpur", "time": "22:00"})
            return result

    def scheduler_loop(self):
        while not self.stop.is_set():
            try:
                self.scheduler_tick()
                self.last_schedule_failure = None
            except Exception as error:
                # Log a changed failure once, retry after a minute without flooding logs.
                detail = f"{type(error).__name__}: {error}"
                if detail != self.last_schedule_failure:
                    self.store.log("scheduled_trigger_failed", {"error": detail})
                    self.last_schedule_failure = detail
            self.stop.wait(30)


def owner_secret(runtime):
    """Legacy tooling helper; web login now verifies the cafe_auth policy."""
    path = Path(runtime) / "owner_secret.json"
    if path.exists():
        return load_json(path)["password"]
    value = secrets.token_urlsafe(24)
    save_json(path, {"password": value})
    return value
