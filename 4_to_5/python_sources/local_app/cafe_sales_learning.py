"""Transparent complete-day sales statistics and server-owned custom purchases.

This is a recency-weighted statistic, not a trained machine-learning model.
Amounts are integer MYR cents. Historical sales do not mutate current stock.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, timedelta
import json
import math
import re

from cafe_core import CafeError, canonical

SALES_FORECAST_SOURCE = "owner_sales_recency_weighted_v1"
EXTRA_UNITS = {"pcs", "box", "bag", "bottle", "kg", "L", "g", "ml", "个", "pack", "carton"}
MAX_SAFE = 2**53 - 1


def _integer(value, maximum=MAX_SAFE):
    return type(value) is int and 0 <= value <= maximum


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CafeError("日期须使用有效的 YYYY-MM-DD。")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CafeError("日期须使用有效的 YYYY-MM-DD。") from None


def normalize_sales_day(data, day, as_of_date):
    from cafe_bom import usage
    today = _day(as_of_date)
    if (not isinstance(day, dict) or not isinstance(day.get("rows"), list)
            or len(day["rows"]) > 100 or _day(day.get("date")) >= today):
        raise CafeError("请录入昨天或更早的完整销量，每天最多100个产品规格。")
    seen, rows = set(), []
    for row in day["rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("productId"), str)
                or row["productId"] not in data["products"]
                or not isinstance(row.get("size"), str)
                or row["size"] not in data["products"][row["productId"]]["recipes"]
                or not _integer(row.get("quantity"), 100000)):
            raise CafeError("销量须使用CSV产品及规格，数量为0至100000的整数。")
        key = (row["productId"], row["size"])
        if key in seen:
            raise CafeError("同一天不能重复录入相同产品规格。")
        seen.add(key)
        rows.append({key: row[key] for key in ("productId", "size", "quantity")})
    rows.sort(key=lambda row: (row["productId"], row["size"]))
    usage(data, rows)
    return {"date": day["date"], "rows": rows}


def normalize_history(data, history, as_of_date):
    if (not isinstance(history, dict) or not _integer(history.get("revision"))
            or not isinstance(history.get("days"), list) or len(history["days"]) > 366):
        raise CafeError("销量历史结构或版本无效，最多保存366天。")
    days = [normalize_sales_day(data, day, as_of_date) for day in history["days"]]
    if len({day["date"] for day in days}) != len(days):
        raise CafeError("销量历史包含重复日期。")
    return {"revision": history["revision"], "days": sorted(days, key=lambda row: row["date"])}


def upsert_sales_day(data, history, day, as_of_date, expected_revision):
    history = normalize_history(data, history, as_of_date)
    if not _integer(expected_revision) or expected_revision != history["revision"]:
        raise CafeError("销量已在其他页面更新，请刷新后重试。")
    day = normalize_sales_day(data, day, as_of_date)
    if next((item for item in history["days"] if item["date"] == day["date"]), None) == day:
        return history
    days = [item for item in history["days"] if item["date"] != day["date"]] + [day]
    if len(days) > 366 or not _integer(history["revision"] + 1):
        raise CafeError("销量历史或版本超出安全范围，最多保存366天。")
    return {"revision": history["revision"] + 1, "days": sorted(days, key=lambda row: row["date"])}


def build_popularity(data, history, as_of_date, half_life_days=7):
    if (type(half_life_days) not in (int, float) or not math.isfinite(half_life_days)
            or not 0 < half_life_days <= 366):
        raise CafeError("半衰期须为0至366天之间的有限正数。")
    history = normalize_history(data, history, as_of_date)
    sizes = [{"productId": pid, "size": size, "group": "bakery" if pid.startswith("BK-") else "beverage",
              "weightedUnits": 0, "dailyAverage": 0, "weight": 0}
             for pid in sorted(data["products"]) for size in sorted(data["products"][pid]["recipes"])]
    by_key = {(row["productId"], row["size"]): row for row in sizes}
    latest = _day(history["days"][-1]["date"]) if history["days"] else _day(as_of_date) - timedelta(days=1)
    weight_total = 0
    for day in history["days"]:
        weight = 2 ** (-(latest - _day(day["date"])).days / half_life_days)
        weight_total += weight
        for row in day["rows"]:
            by_key[(row["productId"], row["size"])]["weightedUnits"] += row["quantity"] * weight
    products_by_id = {}
    for row in sizes:
        row["dailyAverage"] = row["weightedUnits"] / weight_total if weight_total else 0
        product = products_by_id.setdefault(row["productId"], {"productId": row["productId"],
            "group": row["group"], "weightedUnits": 0, "dailyAverage": 0, "weight": 0, "rank": None})
        product["weightedUnits"] += row["weightedUnits"]
        product["dailyAverage"] += row["dailyAverage"]
    products = list(products_by_id.values())
    for group in ("beverage", "bakery"):
        rows = sorted((row for row in products if row["group"] == group),
                      key=lambda row: (-row["weightedUnits"], row["productId"]))
        total = sum(row["weightedUnits"] for row in rows)
        rank = None
        for index, row in enumerate(rows):
            row["weight"] = row["weightedUnits"] / total if total else 0
            if row["weightedUnits"] > 0:
                if not index or row["weightedUnits"] != rows[index-1]["weightedUnits"]:
                    rank = index + 1
                row["rank"] = rank
        for row in sizes:
            if row["group"] == group:
                row["weight"] = row["weightedUnits"] / total if total else 0
    products.sort(key=lambda row: (row["group"], -row["weight"], row["productId"]))
    count = len(history["days"])
    return {"source": SALES_FORECAST_SOURCE, "historyRevision": history["revision"], "sampleDays": count,
            "lastDate": history["days"][-1]["date"] if count else None, "asOfDate": as_of_date,
            "halfLifeDays": half_life_days, "confidence": "low" if count < 7 else "medium" if count < 28 else "higher",
            "staleDays": (_day(as_of_date) - latest).days if count else None,
            "weightReferenceDate": history["days"][-1]["date"] if count else None,
            "method": "Complete-day recency-weighted mean; no simulated prior. Beverage and bakery shares are separate.",
            "products": products, "sizes": sizes}


def forecast_from_history(data, context, history, as_of_date):
    from cafe_bom import forecast_menu, usage
    baseline = forecast_menu(data, context)
    history = normalize_history(data, history, as_of_date)
    if not history["days"]:
        return baseline
    analysis = build_popularity(data, history, as_of_date)
    sales = [{"productId": row["productId"], "size": row["size"],
              "quantity": math.ceil(row["dailyAverage"] * baseline["coefficient"])} for row in analysis["sizes"]]
    if any(not _integer(row["quantity"]) for row in sales):
        raise CafeError("历史销量预测超出安全整数范围。")
    usage(data, sales)
    return {"targetDate": baseline["targetDate"], "coefficient": baseline["coefficient"],
            "source": SALES_FORECAST_SOURCE, "contextSignature": baseline["contextSignature"], "sales": sales,
            "historyRevision": history["revision"], "sampleDays": analysis["sampleDays"],
            "confidence": analysis["confidence"], "asOfDate": as_of_date,
            "contextSnapshot": {key: deepcopy(context[key]) for key in
                ("targetDate", "forecastCups", "referenceCups", "signature", "weather", "holiday")},
            "historySnapshot": history}


def normalize_extra_purchases(data, items):
    if not isinstance(items, list) or len(items) > 20:
        raise CafeError("最多添加20项店铺加购产品。")
    seen, rows = set(), []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"custom_[A-Za-z0-9_-]{1,64}", item["id"])
                or item["id"] in seen or item["id"] in data["materials"] or item["id"] in data["products"]):
            raise CafeError("加购产品编号无效或重复。")
        name = item.get("name")
        if (not isinstance(name, str) or not 0 < len(name.strip()) <= 80
                or any(ord(c) < 32 or ord(c) == 127 or c in "\u2028\u2029" for c in name)):
            raise CafeError("产品名称须为1至80个可见字符。")
        if (not _integer(item.get("quantity"), 10000) or not _integer(item.get("unitPriceCents"), 1000000)
                or not isinstance(item.get("unit"), str) or item["unit"] not in EXTRA_UNITS):
            raise CafeError("加购数量须为0至10000的整数，单价须为整数分，且使用有效采购单位。")
        seen.add(item["id"])
        rows.append({"id": item["id"], "name": name.strip(), "quantity": item["quantity"],
                     "unit": item["unit"], "unitPriceCents": item["unitPriceCents"]})
    return rows


def price_extra_purchases(data, items):
    rows = normalize_extra_purchases(data, items)
    total = 0
    for row in rows:
        row["lineTotalCents"] = row["quantity"] * row["unitPriceCents"]
        total += row["lineTotalCents"]
        if not _integer(total):
            raise CafeError("加购总金额超出安全整数范围。")
    return {"currency": "MYR", "items": rows, "totalCents": total}


class SalesLearningRepository:
    def __init__(self, store, data):
        self.store, self.data = store, data
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS bom_learning (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                history_revision INTEGER NOT NULL, history TEXT NOT NULL,
                extras_revision INTEGER NOT NULL, extras TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO bom_learning VALUES(1,0,'[]',0,'[]')")

    def _state(self, db):
        row = db.execute("SELECT * FROM bom_learning WHERE singleton=1").fetchone()
        if not row:
            raise CafeError("销量与加购数据未初始化。")
        return {"sales_history": {"revision": row["history_revision"], "days": json.loads(row["history"])},
                "extras": {"revision": row["extras_revision"], "items": normalize_extra_purchases(self.data, json.loads(row["extras"]))}}

    def save_history(self, day, expected_revision, as_of_date, request_id=""):
        with self.store.connect() as db:
            old = self._state(db)["sales_history"]
            result = upsert_sales_day(self.data, old, day, as_of_date, expected_revision)
            if result != old:
                changed = db.execute("UPDATE bom_learning SET history_revision=?,history=? WHERE singleton=1 AND history_revision=?",
                    (result["revision"], canonical(result["days"]), expected_revision)).rowcount
                if changed != 1:
                    raise CafeError("销量版本冲突，请刷新后重试。")
                db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            self.store.event(db, "owner", "bom_history_saved", request_id or "bom_history",
                             {"request_id": request_id, "date": day["date"], "history_revision": result["revision"],
                              "inventory_changed": False})
            return result

    def save_extras(self, items, expected_revision, request_id=""):
        items = normalize_extra_purchases(self.data, items)
        with self.store.connect() as db:
            old = self._state(db)["extras"]
            if not _integer(expected_revision) or expected_revision != old["revision"]:
                raise CafeError("加购清单已在其他页面更新，请刷新后重试。")
            revision = expected_revision
            if items != old["items"]:
                revision += 1
                if not _integer(revision):
                    raise CafeError("加购版本超出安全整数范围。")
                changed = db.execute("UPDATE bom_learning SET extras_revision=?,extras=? WHERE singleton=1 AND extras_revision=?",
                                    (revision, canonical(items), expected_revision)).rowcount
                if changed != 1:
                    raise CafeError("加购版本冲突，请刷新后重试。")
                db.execute("UPDATE bom_proposals SET status='superseded',revision=revision+1 WHERE status='pending'")
            self.store.event(db, "owner", "bom_extras_saved", request_id or "bom_extras",
                             {"request_id": request_id, "extras_revision": revision, "inventory_changed": False})
            return {"revision": revision, "items": items}
