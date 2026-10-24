"""CSV-grounded BOM assistant and durable, isolated simulated-store workflow.

No external APIs are called here. Quantities use integer g/ml/individual-item
base units; water/ice have no invented safety level and prices remain unknown.
"""
from __future__ import annotations

import copy
import json
import math
import re
import uuid
from datetime import date, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path

from cafe_core import CafeError, canonical, fingerprint, now

MAX_SAFE = 2**53 - 1
FORECAST_SOURCE = "synthetic_menu_mix_scaled_by_context"
MENU_BASELINE = {
    "BV-002": 12, "BV-003": 20, "BV-004": 12, "BV-005": 10, "BV-006": 8,
    "BV-010": 8, "BV-011": 8, "BV-013": 20, "BV-015": 8, "BV-016": 8,
    "BV-017": 10, "BV-020": 10, "BV-022": 10, "BV-023": 8, "BV-024": 8,
    "BK-01": 20, "BK-02": 10, "BK-05": 14, "BK-06": 10, "BK-08": 6,
}


def integer(value):
    return type(value) is int and 0 <= value <= MAX_SAFE


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def text(value, maximum=10000):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum and "\x00" not in value


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _materials(data):
    if not isinstance(data, dict) or not isinstance(data.get("materials"), dict) or not data["materials"]:
        raise CafeError("BOM原料数据无效。")
    if not isinstance(data.get("products"), dict) or not data["products"]:
        raise CafeError("BOM产品数据无效。")
    for key, material in data["materials"].items():
        if (not text(key) or not isinstance(material, dict)
                or not text(material.get("name"), 200)
                or material.get("unit") not in ("g", "ml", "个")
                or not integer(material.get("initial"))
                or (material.get("safety") is not None and not integer(material.get("safety")))
                or "safety" not in material):
            raise CafeError("BOM原料单位、库存或安全线配置无效。")
    return data["materials"]


def initial_stock(data):
    return {key: material["initial"] for key, material in _materials(data).items()}


def _check_stock(data, stock):
    if not isinstance(stock, dict):
        raise CafeError("库存数据无效。")
    if set(stock) != set(_materials(data)) or not all(integer(n) for n in stock.values()):
        raise CafeError("库存必须包含全部原料的非负整数基础单位数量。")


def usage(data, sales):
    materials = _materials(data)
    if not isinstance(sales, list):
        raise CafeError("销量必须是产品明细数组。")
    result = {}
    for sale in sales:
        if (not isinstance(sale, dict) or not isinstance(sale.get("productId"), str)
                or sale["productId"] not in data["products"] or not integer(sale.get("quantity"))
                or not isinstance(sale.get("size"), str)):
            raise CafeError("产品、规格或数量无效。")
        product = data["products"][sale["productId"]]
        recipes = product.get("recipes") if isinstance(product, dict) else None
        if not isinstance(recipes, dict) or sale["size"] not in recipes:
            raise CafeError("产品、规格或数量无效。")
        recipe = recipes[sale["size"]]
        if not isinstance(recipe, dict) or not recipe:
            raise CafeError("BOM配方为空或无效。")
        for key, per_unit in recipe.items():
            if key not in materials or not integer(per_unit) or per_unit <= 0:
                raise CafeError("非零配料未映射或配方数量无效。")
            quantity = result.get(key, 0) + per_unit * sale["quantity"]
            if not integer(quantity):
                raise CafeError("配方数量超出安全整数范围。")
            result[key] = quantity
    return result


def sell(data, stock, sale):
    if not isinstance(sale, dict) or not integer(sale.get("quantity")) or sale["quantity"] <= 0:
        raise CafeError("售出数量须为正整数。")
    _check_stock(data, stock)
    consumed = usage(data, [sale])
    for key, quantity in consumed.items():
        if stock[key] < quantity:
            raise CafeError(data["materials"][key]["name"] + "库存不足，整单未扣减。")
    after = dict(stock)
    for key, quantity in consumed.items():
        after[key] -= quantity
    return after


def low_stock(data, stock):
    _check_stock(data, stock)
    return [key for key, material in data["materials"].items()
            if material["safety"] is not None and stock[key] < material["safety"]]


def replenish(data, stock, tomorrow_sales):
    _check_stock(data, stock)
    forecast = usage(data, tomorrow_sales)
    result = []
    for key, material in data["materials"].items():
        if material["safety"] is None:
            continue
        required = forecast.get(key, 0) + material["safety"]
        if not integer(required):
            raise CafeError("预计用量超出安全整数范围。")
        result.append({
            "id": key, "name": material["name"], "unit": material["unit"],
            "current": stock[key], "safety": material["safety"],
            "tomorrow": forecast.get(key, 0), "quantity": max(0, required - stock[key]),
        })
    return result


def forecast_menu(data, context):
    """Scale explicit simulated menu assumptions; this does not train 20 SKUs."""
    if not isinstance(context, dict):
        raise CafeError("天气、假日或预测上下文无效。")
    weather, holiday = context.get("weather"), context.get("holiday")
    if (not valid_date(context.get("targetDate"))
            or not finite(context.get("forecastCups")) or context["forecastCups"] < 0
            or not finite(context.get("referenceCups")) or context["referenceCups"] <= 0
            or not text(context.get("signature"))
            or not isinstance(weather, dict) or not finite(weather.get("temperatureC"))
            or not finite(weather.get("precipitationMm")) or weather["precipitationMm"] < 0
            or not isinstance(holiday, dict) or type(holiday.get("isHoliday")) is not bool):
        raise CafeError("天气、假日或预测上下文无效。")
    coefficient = context["forecastCups"] / context["referenceCups"]
    if not math.isfinite(coefficient):
        raise CafeError("预测系数超出范围。")
    sales = []
    for key, baseline in MENU_BASELINE.items():
        scaled = baseline * coefficient
        if not math.isfinite(scaled):
            raise CafeError("预计数量超出范围。")
        sales.append({"productId": key, "size": "份" if key.startswith("BK-") else "中杯",
                      "quantity": math.ceil(scaled)})
    usage(data, sales)
    return {"targetDate": context["targetDate"], "coefficient": coefficient,
            "source": FORECAST_SOURCE, "contextSignature": context["signature"], "sales": sales}


def load_bom_data(path=None):
    source = Path(path) if path else Path(__file__).resolve().parent / "data" / "bom-data.json"
    if source.stat().st_size > 1_000_000:
        raise CafeError("BOM文件超过大小限制。")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    _materials(data)
    for product_id, product in data["products"].items():
        if not text(product_id) or not isinstance(product, dict) or not text(product.get("name"), 200):
            raise CafeError("产品配置无效。")
        recipes = product.get("recipes")
        if not isinstance(recipes, dict) or not recipes:
            raise CafeError("产品配方为空。")
        for size in recipes:
            usage(data, [{"productId": product_id, "size": size, "quantity": 1}])
    return data


class BOMRepository:
    """BOM tables share WorkflowStore's BEGIN IMMEDIATE and audit-event transaction."""

    def __init__(self, store, data):
        self.store = store
        self.data = copy.deepcopy(data)
        from cafe_bom_pricing import BOMPricingRepository
        self.pricing = BOMPricingRepository(store, self.data)
        from cafe_sales_learning import SalesLearningRepository
        self.learning = SalesLearningRepository(store, self.data)
        initial = initial_stock(self.data)
        for product_id, product in self.data["products"].items():
            recipes = product.get("recipes") if isinstance(product, dict) else None
            if not text(product_id) or not isinstance(recipes, dict) or not recipes:
                raise CafeError("产品配方配置无效。")
            for size in recipes:
                usage(self.data, [{"productId": product_id, "size": size, "quantity": 1}])
        self.data_hash = fingerprint(self.data)
        with self.store.connect() as db:
            for statement in (
                """CREATE TABLE IF NOT EXISTS bom_state (
                   singleton INTEGER PRIMARY KEY CHECK(singleton=1), data_version TEXT NOT NULL,
                   stock TEXT NOT NULL, revision INTEGER NOT NULL,
                   context_hash TEXT NOT NULL DEFAULT '', context_revision INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS bom_requests (
                   request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, document TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bom_last_transaction (
                   singleton INTEGER PRIMARY KEY CHECK(singleton=1), document TEXT)""",
                """CREATE TABLE IF NOT EXISTS bom_undo_requests (
                   request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, document TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bom_proposals (
                   id TEXT PRIMARY KEY, input_hash TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                   revision INTEGER NOT NULL, stock_revision INTEGER NOT NULL,
                   context_signature TEXT NOT NULL, document TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bom_orders (
                   id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL,
                   signature TEXT UNIQUE NOT NULL, document TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bom_drafts (
                   id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES bom_orders(id),
                   document TEXT NOT NULL)""",
            ):
                db.execute(statement)
            db.execute("INSERT OR IGNORE INTO bom_state(singleton,data_version,stock,revision) VALUES(1,?,?,0)",
                       (self.data_hash, canonical(initial)))
            state = self._state(db)
            if state["data_version"] != self.data_hash:
                raise CafeError("BOM数据版本已改变，请人工迁移库存；不会自动重置现有库存。")
            self._initialize_last_transaction(db, state)

    def _state(self, db):
        row = db.execute("SELECT * FROM bom_state WHERE singleton=1").fetchone()
        if not row:
            raise CafeError("BOM库存未初始化。")
        state = dict(row)
        state["stock"] = json.loads(state["stock"])
        _check_stock(self.data, state["stock"])
        state["pricebook"] = self.pricing._state(db)
        state.update(self.learning._state(db))
        return state


    def _transaction_from_sale(self, result, request_id, payload_hash, state):
        """Validate a durable sale before exposing it as an undo candidate."""
        if (not isinstance(result, dict) or result.get("request_id") != request_id
                or not text(request_id, 200) or not integer(result.get("revision"))
                or result["revision"] <= 0 or result["revision"] != state["revision"]
                or result.get("stock") != state["stock"]):
            raise CafeError("售出记录与当前库存不一致，不能撤销。")
        _check_stock(self.data, result.get("stock"))
        sale = result.get("sale")
        if not isinstance(sale, dict) or not integer(sale.get("quantity")) or sale["quantity"] <= 0:
            raise CafeError("售出记录无效，不能撤销。")
        consumed = usage(self.data, [sale])
        recorded = result.get("consumed")
        if (not isinstance(recorded, dict) or recorded != consumed
                or any(not integer(n) or n <= 0 for n in recorded.values())
                or payload_hash != fingerprint({"sale": sale, "revision": result["revision"] - 1})):
            raise CafeError("售出扣减记录无效，不能撤销。")
        return {"transaction_id": request_id, "sale": copy.deepcopy(sale),
                "consumed": dict(consumed), "stock_revision": result["revision"]}

    def _initialize_last_transaction(self, db, state):
        # A present NULL marker means undo has already been used or migration was
        # conservatively declined. Never reconstruct an older transaction later.
        if db.execute("SELECT 1 FROM bom_last_transaction WHERE singleton=1").fetchone():
            return
        transaction = None
        event = db.execute("SELECT subject,detail FROM events WHERE action='bom_sale_recorded' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        if event:
            request = db.execute("SELECT * FROM bom_requests WHERE request_id=?", (event["subject"],)).fetchone()
            if request:
                try:
                    result, detail = json.loads(request["document"]), json.loads(event["detail"])
                    candidate = self._transaction_from_sale(result, request["request_id"],
                                                            request["payload_hash"], state)
                    if (not isinstance(detail, dict) or canonical(detail.get("sale")) != canonical(candidate["sale"])
                            or canonical(detail.get("consumed")) != canonical(candidate["consumed"])
                            or type(detail.get("revision")) is not int
                            or detail["revision"] != candidate["stock_revision"]):
                        raise CafeError("审计记录与售出记录不一致。")
                    transaction = candidate
                except (CafeError, ValueError, TypeError, KeyError):
                    # Legacy data without complete matching evidence is not
                    # guessed. All new successful sales enable undo normally.
                    transaction = None
        db.execute("INSERT INTO bom_last_transaction(singleton,document) VALUES(1,?)",
                   (canonical(transaction) if transaction is not None else None,))

    def _last_transaction(self, db, state):
        row = db.execute("SELECT document FROM bom_last_transaction WHERE singleton=1").fetchone()
        if not row or row["document"] is None:
            return None
        transaction = json.loads(row["document"])
        if (not isinstance(transaction, dict) or not text(transaction.get("transaction_id"), 200)
                or not integer(transaction.get("stock_revision"))):
            raise CafeError("最近售出记录无效，请刷新后人工核对库存。")
        return dict(transaction, can_undo=transaction["stock_revision"] == state["revision"])

    def undo_sale(self, revision, request_id, transaction_id):
        """Undo exactly the last committed sale once, with CAS and durable retries."""
        if (not integer(revision) or not text(request_id, 200)
                or not text(transaction_id, 200)):
            raise CafeError("撤销版本、请求编号或售出编号无效。")
        payload_hash = fingerprint({"revision": revision, "transaction_id": transaction_id})
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM bom_undo_requests WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise CafeError("同一撤销请求编号不能用于不同售出记录或库存版本。")
                return json.loads(previous["document"])
            state = self._state(db)
            if state["revision"] != revision:
                raise CafeError("库存版本已更新，不能撤销旧页面中的售出记录。")
            transaction = self._last_transaction(db, state)
            if (not transaction or not transaction["can_undo"]
                    or transaction["transaction_id"] != transaction_id):
                raise CafeError("该售出已撤销或不是最近一次成功售出，请刷新页面。")
            stored = db.execute("SELECT * FROM bom_requests WHERE request_id=?", (transaction_id,)).fetchone()
            if not stored:
                raise CafeError("找不到可核验的原售出记录。")
            verified = self._transaction_from_sale(json.loads(stored["document"]), transaction_id,
                                                   stored["payload_hash"], state)
            if any(transaction.get(key) != verified[key]
                   for key in ("transaction_id", "sale", "consumed", "stock_revision")):
                raise CafeError("最近售出记录与原始扣减不一致，不能撤销。")
            next_revision = revision + 1
            if not integer(next_revision):
                raise CafeError("库存版本超出安全整数范围。")
            stock = dict(state["stock"])
            for key, quantity in verified["consumed"].items():
                restored = stock[key] + quantity
                if not integer(restored):
                    raise CafeError("撤销后的库存超出安全整数范围，整单未恢复。")
                stock[key] = restored
            result = {"request_id": request_id, "transaction_id": transaction_id,
                      "sale": verified["sale"], "restored": verified["consumed"],
                      "stock": stock, "revision": next_revision, "last_transaction": None}
            db.execute("UPDATE bom_state SET stock=?,revision=? WHERE singleton=1",
                       (canonical(stock), next_revision))
            db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            db.execute("UPDATE bom_last_transaction SET document=NULL WHERE singleton=1")
            db.execute("INSERT INTO bom_undo_requests VALUES(?,?,?)", (request_id, payload_hash, canonical(result)))
            self.store.event(db, "owner", "bom_sale_undone", request_id, {
                "transaction_id": transaction_id, "sale": verified["sale"],
                "restored": verified["consumed"], "revision": next_revision,
                "message": "已撤销上一次模拟售出，恢复原始扣减数量；历史采购审批不变。",
            })
            return result

    def _forecast(self, context, state):
        from cafe_sales_learning import forecast_from_history
        forecast_menu(self.data, context)  # Validate untrusted context before reading fields.
        as_of_date = context.get("asOfDate") or (date.fromisoformat(context["targetDate"]) - timedelta(days=1)).isoformat()
        return forecast_from_history(self.data, context, state["sales_history"], as_of_date)

    def _synchronize_context(self, db, forecast):
        digest = fingerprint(forecast)
        state = self._state(db)
        if state["context_hash"] != digest:
            db.execute("UPDATE bom_state SET context_hash=?,context_revision=context_revision+1 WHERE singleton=1",
                       (digest,))
            db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            self.store.event(db, "system", "bom_context_updated", "bom",
                             {"context_signature": forecast["contextSignature"], "source": forecast["source"]})
            state = self._state(db)
        return state

    def _input_hash(self, state, forecast):
        return fingerprint({"data": self.data_hash, "stock": state["stock"],
                            "stock_revision": state["revision"], "context_revision": state["context_revision"],
                            "pricebook": state["pricebook"], "forecast": forecast,
                            "sales_history": state["sales_history"], "extras": state["extras"]})

    @staticmethod
    def _proposal_row(row):
        return dict(json.loads(row["document"]), status=row["status"], revision=row["revision"])

    def snapshot(self, context=None):
        with self.store.connect() as db:
            state = self._state(db)
            forecast = self._forecast(context, state) if context is not None else None
            state = self._synchronize_context(db, forecast) if forecast is not None else state
            orders = [json.loads(row[0]) for row in db.execute(
                "SELECT document FROM bom_orders ORDER BY rowid DESC LIMIT 50")]
            active = next((order["id"] for order in orders
                           if forecast is not None and self._order_is_current(order, state, forecast)), None)
            return {
                "last_transaction": self._last_transaction(db, state),
                "active_order_id": active,
                "current_input_hash": self._input_hash(state, forecast) if forecast is not None else None,
                "context_signature": forecast["contextSignature"] if forecast is not None else None,
                "stock": state["stock"], "revision": state["revision"], "data_version": self.data_hash,
                "pricebook": copy.deepcopy(state["pricebook"]),
                "sales_history": copy.deepcopy(state["sales_history"]), "extras": copy.deepcopy(state["extras"]),
                "context_revision": state["context_revision"],
                "proposals": [self._proposal_row(row) for row in db.execute(
                    "SELECT * FROM bom_proposals ORDER BY rowid DESC LIMIT 50")],
                "orders": orders,
                "drafts": [json.loads(row[0]) for row in db.execute(
                    "SELECT document FROM bom_drafts ORDER BY rowid DESC LIMIT 50")],
                "events": [dict(row) for row in db.execute(
                    "SELECT * FROM events WHERE action LIKE 'bom_%' ORDER BY id DESC LIMIT 100")],
            }

    def simulate_sale(self, sale, revision, request_id):
        if not integer(revision) or not text(request_id, 200):
            raise CafeError("库存版本或售出请求编号无效。")
        consumed = usage(self.data, [sale])
        if not isinstance(sale, dict) or not integer(sale.get("quantity")) or sale["quantity"] <= 0:
            raise CafeError("售出数量须为正整数。")
        normalized_sale = {key: sale[key] for key in ("productId", "size", "quantity")}
        payload_hash = fingerprint({"sale": normalized_sale, "revision": revision})
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM bom_requests WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise CafeError("同一售出请求编号不能用于不同数量或库存版本。")
                return json.loads(previous["document"])
            state = self._state(db)
            if revision != state["revision"]:
                raise CafeError("库存版本已更新，请刷新后重新确认售出数量。")
            if not integer(revision + 1):
                raise CafeError("库存版本超出安全整数范围。")
            stock = sell(self.data, state["stock"], normalized_sale)
            result = {"request_id": request_id, "sale": normalized_sale, "consumed": consumed,
                      "stock": stock, "revision": revision + 1}
            db.execute("UPDATE bom_state SET stock=?,revision=? WHERE singleton=1",
                       (canonical(stock), result["revision"]))
            db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            db.execute("INSERT INTO bom_requests VALUES(?,?,?)", (request_id, payload_hash, canonical(result)))
            transaction = {"transaction_id": request_id, "sale": normalized_sale,
                           "consumed": consumed, "stock_revision": result["revision"]}
            db.execute("UPDATE bom_last_transaction SET document=? WHERE singleton=1", (canonical(transaction),))
            self.store.event(db, "owner", "bom_sale_recorded", request_id,
                             {"sale": normalized_sale, "consumed": consumed, "revision": result["revision"]})
            return result

    def propose(self, context):
        with self.store.connect() as db:
            state = self._state(db)
            forecast = self._forecast(context, state)
            state = self._synchronize_context(db, forecast)
            input_hash = self._input_hash(state, forecast)
            old = db.execute("SELECT * FROM bom_proposals WHERE input_hash=?", (input_hash,)).fetchone()
            if old:
                return self._proposal_row(old)
            from cafe_bom_pricing import apply_prices
            pricing = apply_prices(replenish(self.data, state["stock"], forecast["sales"]), pricebook=state["pricebook"])
            rows = pricing["items"]
            from cafe_sales_learning import price_extra_purchases
            extras = price_extra_purchases(self.data, state["extras"]["items"])
            extras["revision"] = state["extras"]["revision"]
            pricing = self._add_extras_pricing(pricing, extras["items"])
            document = {
                "id": "BOM-PROP-" + str(uuid.uuid4()), "input_hash": input_hash, "status": "pending",
                "revision": 1, "stock_revision": state["revision"],
                "context_signature": forecast["contextSignature"], "target_date": forecast["targetDate"],
                "forecast": forecast, "rows": rows, "pricebook": copy.deepcopy(state["pricebook"]),
                "extras": extras, "sales_history_revision": state["sales_history"]["revision"],
                "pricing": {key: value for key, value in pricing.items() if key not in ("items", "pricebook_snapshot")},
            }
            db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            db.execute("INSERT INTO bom_proposals VALUES(?,?,?,?,?,?,?)",
                       (document["id"], input_hash, "pending", 1, state["revision"],
                        forecast["contextSignature"], canonical(document)))
            self.store.event(db, "system", "bom_approval_requested", document["id"],
                             {"input_hash": input_hash, "stock_revision": state["revision"],
                              "source": forecast["source"]})
            return document

    def proposal(self, proposal_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM bom_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                raise CafeError("BOM采购建议不存在。")
            return self._proposal_row(row)

    def _pending(self, db, proposal_id, revision, input_hash, context):
        if not integer(revision) or not text(input_hash, 200):
            raise CafeError("审批版本或输入指纹无效。")
        row = db.execute("SELECT * FROM bom_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise CafeError("BOM建议已处理或过期，请重新生成。")
        document = self._proposal_row(row)
        state = self._state(db)
        forecast = self._forecast(context, state)
        if (row["revision"] != revision or row["input_hash"] != input_hash
                or state["revision"] != row["stock_revision"]
                or state["context_hash"] != fingerprint(forecast)
                or self._input_hash(state, forecast) != input_hash):
            raise CafeError("库存、销量、加购、价格、天气或假日已改变，请重新生成建议后审批。")
        return document, state

    def _items(self, document, quantities):
        if not isinstance(quantities, dict):
            raise CafeError("采购数量须为按原料ID填写的整数对象。")
        rows = document["rows"]
        extras = document.get("extras", {}).get("items", [])
        known = {row["id"] for row in rows} | {row["id"] for row in extras}
        if any(key not in known or not integer(value) for key, value in quantities.items()):
            raise CafeError("未知原料或无效采购数量。")
        items = []
        for row in rows:
            approved = quantities.get(row["id"], row["quantity"])
            shortage = max(0, row["quantity"] - approved)
            items.append({
                "ingredient_id": row["id"], "name": row["name"], "unit": row["unit"],
                "current_stock": row["current"], "safety_stock": row["safety"],
                "tomorrow_usage": row["tomorrow"], "suggested_quantity": row["quantity"],
                "approved_quantity": approved, "remaining_shortage": shortage, "shortage": shortage,
                "approval_warnings": ["低于建议数量，仍有补货缺口。"] if shortage else [],
                "supplier_id": "BOM_DEMO", "unit_price_cents": None, "line_total_cents": None,
            })
        from cafe_bom_pricing import apply_prices
        items = apply_prices(items, pricebook=document.get("pricebook"))["items"]
        for row in extras:
            approved = quantities.get(row["id"], row["quantity"])
            if approved > 10000:
                raise CafeError("加购数量须为0至10000的整数。")
            amount = approved * row["unitPriceCents"]
            items.append({"ingredient_id": row["id"], "name": row["name"], "unit": row["unit"],
                "item_type": "custom_purchase", "suggested_quantity": row["quantity"],
                "approved_quantity": approved, "remaining_shortage": 0, "shortage": 0,
                "approval_warnings": [], "supplier_id": "BOM_DEMO",
                "unit_price_cents": row["unitPriceCents"], "line_total_cents": amount,
                "price_known": True, "price_source": "owner_entry", "currency": "MYR",
                "base_quantity": approved, "purchase_packs": approved, "pack_count": approved,
                "pack_quantity": 1, "pack_price_cents": row["unitPriceCents"], "purchase_quantity": approved})
        return items

    @staticmethod
    def _add_extras_pricing(pricing, extras):
        total = sum(row.get("lineTotalCents", row.get("line_total_cents", 0)) for row in extras)
        if not integer(total + pricing["known_subtotal_cents"]):
            raise CafeError("采购总金额超出安全整数范围。")
        result = dict(pricing)
        result["known_subtotal_cents"] += total
        result["total_cents"] = result["known_subtotal_cents"] if result["prices_complete"] else None
        result["extras_total_cents"] = total
        return result

    def _combined_pricing(self, items, document):
        from cafe_bom_pricing import apply_prices
        bom = [row for row in items if row.get("item_type") != "custom_purchase"]
        extras = [row for row in items if row.get("item_type") == "custom_purchase"]
        pricing = self._add_extras_pricing(apply_prices(bom, pricebook=document.get("pricebook")), extras)
        pricing["items"] += copy.deepcopy(extras)
        return pricing

    def preview(self, proposal_id, revision, input_hash, quantities, context):
        with self.store.connect() as db:
            document, state = self._pending(db, proposal_id, revision, input_hash, context)
            items = self._items(document, quantities)
            pricing = self._combined_pricing(items, document)
            return {
                "id": proposal_id, "revision": revision, "input_hash": input_hash,
                "stock_revision": state["revision"], "items": items,
                "requires_acknowledgement": any(item["remaining_shortage"] for item in items),
                "changes": [item for item in items if item["approved_quantity"] != item["suggested_quantity"]],
                "total_cents": pricing["total_cents"], "prices_complete": pricing["prices_complete"],
                "missing_price_ids": pricing["missing_price_ids"],
                "known_subtotal_cents": pricing["known_subtotal_cents"], "extras_total_cents": pricing["extras_total_cents"],
            }

    def approve(self, proposal_id, revision, input_hash, quantities, context, acknowledged=False):
        if type(acknowledged) is not bool or not integer(revision) or not text(input_hash, 200):
            raise CafeError("审批确认、版本或输入指纹无效。")
        with self.store.connect() as db:
            previous = db.execute("SELECT * FROM bom_orders WHERE proposal_id=?", (proposal_id,)).fetchone()
            if previous:
                old = json.loads(previous["document"])
                proposal = db.execute("SELECT * FROM bom_proposals WHERE id=?", (proposal_id,)).fetchone()
                document = json.loads(proposal["document"])
                items = self._items(document, quantities)
                signature = fingerprint({"proposal_id": proposal_id,
                                         "quantities": {item["ingredient_id"]: item["approved_quantity"] for item in items}})
                if (old["signature"] != signature or input_hash != old["input_hash"]
                        or revision != document["revision"]):
                    raise CafeError("此建议已经审批；不同采购数量不能覆盖已生成订单。")
                return old

            document, state = self._pending(db, proposal_id, revision, input_hash, context)
            items = self._items(document, quantities)
            if not any(item["approved_quantity"] > 0 for item in items):
                raise CafeError("采购数量全部为零，不能生成空订单。")
            if any(item["remaining_shortage"] for item in items) and not acknowledged:
                raise CafeError("采购量低于建议；请明确确认剩余缺口后审批。")
            signature = fingerprint({"proposal_id": proposal_id,
                                     "quantities": {item["ingredient_id"]: item["approved_quantity"] for item in items}})
            order = {
                "id": "DEMO-BOM-" + str(uuid.uuid4()), "proposal_id": proposal_id,
                "signature": signature, "input_hash": input_hash,
                "target_date": document["target_date"], "stock_revision": state["revision"],
                "approved_at": now(), "approved_by": "owner", "status": "draft",
                "items": items, "original_items": document["rows"], "forecast": document["forecast"],
                "currency": "MYR", "total_cents": None, "prices_complete": False,
                "inventory_changed": False, "sent": False,
            }
            pricing = self._combined_pricing(items, document)
            order.update(items=pricing["items"], total_cents=pricing["total_cents"],
                prices_complete=pricing["prices_complete"], missing_price_ids=pricing["missing_price_ids"],
                price_revision=pricing["price_revision"], pricebook_snapshot=pricing["pricebook_snapshot"],
                known_subtotal_cents=pricing["known_subtotal_cents"], extras_total_cents=pricing["extras_total_cents"],
                extras_snapshot=copy.deepcopy(document.get("extras", {})))
            draft = self._build_draft(order)
            db.execute("INSERT INTO bom_orders VALUES(?,?,?,?)",
                       (order["id"], proposal_id, signature, canonical(order)))
            db.execute("INSERT INTO bom_drafts VALUES(?,?,?)",
                       (draft["id"], order["id"], canonical(draft)))
            db.execute("UPDATE bom_proposals SET status='approved',revision=revision+1 WHERE id=?", (proposal_id,))
            self.store.event(db, "owner", "bom_approved_draft_created", proposal_id, {
                "message": "[" + order["approved_at"] + "] 店长已审批通过补货建议",
                "order_id": order["id"], "draft_ids": [draft["id"]],
                "items": items, "inventory_changed": False, "sent": False,
            })
            return order

    @staticmethod
    def _build_draft(order):
        money = lambda cents: str(cents // 100) + "." + format(cents % 100, "02d")
        subject = "[DEMO BOM] Cafe replenishment - " + order["target_date"]
        lines = [
            "SIMULATED PURCHASE DRAFT — manager approved",
            "Order: " + order["id"], "Required date: " + order["target_date"],
            ("Forecast: entered complete-day sales, seven-day half-life weighted mean, scaled once by weather/holiday context."
                if order.get("forecast", {}).get("source") == "owner_sales_recency_weighted_v1" else
                "Forecast: fixed simulated menu scaled by synthetic weather/holiday context."),
            "This is a transparent statistic, not a trained forecast for the 20 CSV products.", "",
        ]
        for item in order["items"]:
            if item["approved_quantity"] > 0:
                line = "- " + item["name"] + ": " + str(item["approved_quantity"]) + " " + item["unit"]
                if item["remaining_shortage"]:
                    line += " (below recommendation by " + str(item["remaining_shortage"]) + " " + item["unit"] + ")"
                if item.get("item_type") == "custom_purchase":
                    line += " | MYR " + money(item["unit_price_cents"]) + "/" + item["unit"] + " = MYR " + money(item["line_total_cents"])
                elif item.get("price_known"):
                    line += (" | purchase " + str(item["purchase_packs"]) + " packs x " + str(item["pack_quantity"])
                        + " " + item["unit"] + " = " + str(item["purchase_quantity"]) + " " + item["unit"]
                        + " | MYR " + money(item["pack_price_cents"])
                        + "/pack = MYR " + money(item["line_total_cents"])
                        + " | quote source: " + str(item["price_source"]))
                else:
                    line += " | price unconfirmed"
                lines.append(line)
        lines += ["", "Quantities use CSV base units; quoted purchases round up to complete packs.",
                  ("Estimated total: MYR " + money(order["total_cents"])
                    if order.get("prices_complete") else "Estimated total: unconfirmed (MYR)")
                  + ". Nothing has been sent."]
        body = "\n".join(lines)
        email = EmailMessage(policy=SMTP)
        email["Subject"] = subject
        email["X-Unsent"] = "1"
        email["To"] = "bom-orders@example.com"
        email.set_content(body)
        return {
            "id": "BOM-DRAFT-" + str(uuid.uuid4()), "order_id": order["id"],
            "supplier_id": "BOM_DEMO", "supplier_name": "BOM demo procurement",
            "email": "bom-orders@example.com", "subject": subject, "body": body,
            "email_eml": email.as_string(), "whatsapp_url": None, "sent": False,
            "currency": "MYR", "total_cents": order.get("total_cents"),
            "prices_complete": order.get("prices_complete", False),
        }

    def order(self, order_id):
        with self.store.connect() as db:
            row = db.execute("SELECT document FROM bom_orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise CafeError("BOM采购订单不存在。")
            return json.loads(row[0])

    def drafts_for(self, order_id):
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT document FROM bom_drafts WHERE order_id=? ORDER BY id", (order_id,))]

    def draft(self, draft_id):
        with self.store.connect() as db:
            row = db.execute("SELECT document FROM bom_drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise CafeError("BOM采购草稿不存在。")
            return json.loads(row[0])

    def _order_is_current(self, order, state, forecast):
        return (order.get("status") == "draft" and order.get("stock_revision") == state["revision"]
                and state["context_hash"] == fingerprint(forecast)
                and order.get("input_hash") == self._input_hash(state, forecast))

    def approved_draft(self, draft_id, context):
        """Resolve a current, genuinely approved BOM draft before an optional external action."""
        with self.store.connect() as db:
            state = self._state(db)
            forecast = self._forecast(context, state)
            row = db.execute("SELECT * FROM bom_drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise CafeError("BOM采购草稿不存在。")
            draft = json.loads(row["document"])
            stored = db.execute("SELECT * FROM bom_orders WHERE id=?", (row["order_id"],)).fetchone()
            if not stored:
                raise CafeError("BOM草稿没有已审批订单。")
            order = json.loads(stored["document"])
            proposal = db.execute("SELECT * FROM bom_proposals WHERE id=?", (order["proposal_id"],)).fetchone()
            state = self._state(db)
            if (not proposal or proposal["status"] != "approved"
                    or proposal["input_hash"] != order["input_hash"]
                    or not self._order_is_current(order, state, forecast)):
                raise CafeError("库存、销量、加购、价格、天气或假日已改变，旧BOM草稿已失效；请重新生成并审批。")
            return draft, order
