"""Synthetic, single-shop bill-of-materials and procurement accounting.

All amounts use the ingredient's base unit (g, ml, pcs), with no implicit unit
conversion. Closing stock is already net of today's sales. This module never
sends an order, changes inventory, or judges whether food is safe to use.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from cafe_core import CafeError

UNITS = {"g", "ml", "pcs"}
ZERO = Decimal("0")
MAX_QUANTITY = Decimal("1000000000")


def _keys(value, required, label, optional=()):
    if not isinstance(value, dict):
        raise CafeError(f"{label}必须是对象。")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise CafeError(f"{label}字段错误，缺少 {sorted(missing)}，未知 {sorted(unknown)}。")


def _text(value, label, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise CafeError(f"{label}必须是非空文本（最多 {limit} 字符，不含控制字符）。")
    return value.strip()


def _day(value, label):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise CafeError(f"{label}必须为 YYYY-MM-DD 日期。") from None


def _quantity(value, label, *, unit=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CafeError(f"{label}必须是数字。")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise CafeError(f"{label}不是有效数字。") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_QUANTITY or (positive and amount == 0):
        raise CafeError(f"{label}必须为{'正数' if positive else '非负数'}，且不超过 {MAX_QUANTITY}。")
    if unit == "pcs" and amount != amount.to_integral_value():
        raise CafeError(f"{label}使用 pcs 时必须为整数。")
    return amount


def _rows(value, label, *, nonempty=True):
    if not isinstance(value, list) or len(value) > 1000 or (nonempty and not value):
        raise CafeError(f"{label}必须是{'非空' if nonempty else ''}列表，最多 1000 行。")
    return value


def _number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def _common(document, label):
    _text(document["shop_id"], f"{label}.shop_id", 80)
    if document["data_source"] != "synthetic":
        raise CafeError(f"{label}仅接受 synthetic 比赛模拟数据。")


def _round_pack(amount, pack):
    return (amount / pack).to_integral_value(rounding=ROUND_CEILING) * pack


def _constraints(item, amount):
    """Return human-readable hard limits for a proposed amount."""
    messages = []
    name = item["name"]
    if amount > _quantity(item["max_order"], "max_order"):
        messages.append(f"{name}采购量超过单次上限。")
    occupancy = _quantity(item["physical_on_hand"], "physical_on_hand") + _quantity(item["due_inbound"], "due_inbound") + amount
    if occupancy > _quantity(item["storage_capacity"], "storage_capacity"):
        messages.append(f"{name}到货时库存超过储存上限（包含未移除的过期/停用批次）。")
    if amount > 0 and _day(item["delivery_date"], "delivery_date") > _day(item["target_date"], "target_date"):
        messages.append(f"{name}新采购无法在目标日开店前送达。")
    return messages


def plan(forecast, inventory, recipes, suppliers, target_date):
    """Build next-opening ingredient orders from a closing-stock snapshot.

    Expected JSON structures are demonstrated in examples_v3/*.json. A batch's
    usable_through is inclusive. Inbound arrival_date means available before
    opening on that date; it is a confirmed, unreceived quantity and must not be
    included in the stock batches. Late inbound does not cover tomorrow's need.
    """
    target = _day(target_date, "target_date")
    _keys(inventory, {"shop_id", "as_of_date", "snapshot_id", "data_source", "items"}, "库存")
    _keys(recipes, {"shop_id", "data_source", "recipes"}, "配方")
    _keys(suppliers, {"shop_id", "data_source", "suppliers"}, "供应商")
    for document, label in ((inventory, "库存"), (recipes, "配方"), (suppliers, "供应商")):
        _common(document, label)
    if len({inventory["shop_id"], recipes["shop_id"], suppliers["shop_id"]}) != 1:
        raise CafeError("库存、配方、供应商必须属于同一家店。")
    as_of = _day(inventory["as_of_date"], "as_of_date")
    if target != as_of + timedelta(days=1):
        raise CafeError("库存必须为目标日前一天的收盘快照；请刷新库存，不能使用陈旧或未来快照。")
    _text(inventory["snapshot_id"], "snapshot_id", 160)
    if not isinstance(forecast, dict) or "items" not in forecast:
        raise CafeError("预测必须含 items 列表。")
    if "target_date" in forecast and forecast["target_date"] != target_date:
        raise CafeError("预测日期与采购目标日期不一致。")
    if "shop_id" in forecast and forecast["shop_id"] != inventory["shop_id"]:
        raise CafeError("预测与库存店铺不一致。")

    supplier_map = {}
    for supplier in _rows(suppliers["suppliers"], "suppliers"):
        _keys(supplier, {"supplier_id", "name", "delivery_date", "email", "whatsapp"}, "供应商行")
        sid = _text(supplier["supplier_id"], "supplier_id", 80)
        if sid in supplier_map:
            raise CafeError("supplier_id 重复。")
        _text(supplier["name"], "供应商名称")
        if _day(supplier["delivery_date"], "delivery_date") < as_of:
            raise CafeError("供应商承诺到货日不能早于库存快照日。")
        # Optional contacts may be empty; any later transport remains a draft.
        for key in ("email", "whatsapp"):
            value = supplier[key]
            if not isinstance(value, str) or len(value) > 250 or any(ord(c) < 32 for c in value):
                raise CafeError(f"供应商 {key} 必须为最多 250 字符且不含控制字符的文本。")
        supplier_map[sid] = supplier

    stock_map = {}
    warnings = []
    for stock in _rows(inventory["items"], "库存 items"):
        _keys(stock, {"ingredient_id", "name", "unit", "batches", "safety_stock", "pack_size", "min_order", "max_order", "storage_capacity", "supplier_id", "inbounds"}, "库存行", optional=("purchase_unit",))
        iid = _text(stock["ingredient_id"], "ingredient_id", 80)
        if iid in stock_map:
            raise CafeError("ingredient_id 重复。")
        name = _text(stock["name"], "食材名称")
        _text(stock.get("purchase_unit", "包"), "采购包装名称", 12)
        unit = stock["unit"]
        if not isinstance(unit, str) or unit not in UNITS:
            raise CafeError("食材单位仅支持 g、ml、pcs，不能隐式混算 kg/L。")
        for key in ("safety_stock", "pack_size", "min_order", "max_order", "storage_capacity"):
            _quantity(stock[key], f"{name}.{key}", unit=unit, positive=key in ("pack_size", "storage_capacity"))
        sid = _text(stock["supplier_id"], "supplier_id", 80)
        if sid not in supplier_map:
            raise CafeError(f"{name}引用了不存在的供应商。")
        available = ZERO
        physical = ZERO
        excluded = ZERO
        for batch in _rows(stock["batches"], "batches", nonempty=False):
            _keys(batch, {"quantity", "usable_through", "blocked"}, "库存批次")
            amount = _quantity(batch["quantity"], f"{name}.batch.quantity", unit=unit)
            end = _day(batch["usable_through"], "usable_through")
            if type(batch["blocked"]) is not bool:
                raise CafeError("blocked 必须为布尔值。")
            physical += amount
            if end < target or batch["blocked"]:
                excluded += amount
            else:
                available += amount
        due = ZERO
        late = ZERO
        for inbound in _rows(stock["inbounds"], "inbounds", nonempty=False):
            _keys(inbound, {"quantity", "arrival_date"}, "在途行")
            amount = _quantity(inbound["quantity"], f"{name}.inbound.quantity", unit=unit)
            arrival = _day(inbound["arrival_date"], "arrival_date")
            if arrival <= as_of:
                raise CafeError(f"{name}在途到货日必须晚于收盘快照日；已到货数量应记入批次，避免重复扣减。")
            if arrival <= target:
                due += amount
            else:
                late += amount
        if excluded:
            warnings.append(f"{name}有 {_number(excluded)} {unit} 过期或停用批次，未计入可用量，但移除前仍占仓容。")
        if late:
            warnings.append(f"{name}有 {_number(late)} {unit} 在途晚于目标日，未抵扣明日缺口。")
        stock_map[iid] = {"stock": stock, "available": available, "physical": physical, "excluded": excluded, "due": due, "late": late}

    recipe_map = {}
    for recipe in _rows(recipes["recipes"], "recipes"):
        _keys(recipe, {"sku", "ingredients"}, "配方行")
        sku = _text(recipe["sku"], "sku", 80)
        if sku in recipe_map:
            raise CafeError("配方 SKU 重复。")
        components = {}
        for component in _rows(recipe["ingredients"], "ingredients"):
            _keys(component, {"ingredient_id", "quantity", "unit"}, "配方食材")
            iid = _text(component["ingredient_id"], "ingredient_id", 80)
            if iid not in stock_map:
                raise CafeError(f"配方 {sku} 引用了库存中不存在的食材 {iid}。")
            if iid in components:
                raise CafeError(f"配方 {sku} 中同一食材重复。")
            unit = stock_map[iid]["stock"]["unit"]
            if component["unit"] != unit:
                raise CafeError(f"配方 {sku} 的 {iid} 单位与库存不一致；请先统一为 {unit}。")
            components[iid] = _quantity(component["quantity"], f"配方 {sku}.{iid}", unit=unit, positive=True)
        recipe_map[sku] = components

    usage = {iid: ZERO for iid in stock_map}
    seen = set()
    total_units = ZERO
    for estimate in _rows(forecast["items"], "预测 items"):
        if not isinstance(estimate, dict) or not {"sku", "forecast_units"} <= estimate.keys():
            raise CafeError("每个预测必须含 sku 和 forecast_units。")
        sku = _text(estimate["sku"], "预测 sku", 80)
        if sku in seen:
            raise CafeError("预测 SKU 重复。")
        if sku not in recipe_map:
            raise CafeError(f"SKU {sku} 缺少配方，不能静默省略食材需求。")
        seen.add(sku)
        units = _quantity(estimate["forecast_units"], f"{sku}.forecast_units")
        total_units += units
        for iid, amount in recipe_map[sku].items():
            usage[iid] += units * amount
    missing_skus = recipe_map.keys() - seen
    if missing_skus:
        raise CafeError(f"在售配方缺少预测：{sorted(missing_skus)}。未售出不能当作缺失数据；请提供显式零预测，或移除停售配方。")
    # Inventory with no forecast usage is still evaluated against safety stock.
    items = []
    blockers = []
    for iid, row in stock_map.items():
        stock = row["stock"]
        safety = _quantity(stock["safety_stock"], "safety_stock")
        shortage = max(ZERO, usage[iid] + safety - row["available"] - row["due"])
        pack = _quantity(stock["pack_size"], "pack_size", positive=True)
        minimum = _quantity(stock["min_order"], "min_order")
        order = _round_pack(max(shortage, minimum), pack) if shortage else ZERO
        supplier = supplier_map[stock["supplier_id"]]
        item = {
            "ingredient_id": iid, "name": stock["name"], "unit": stock["unit"],
            "purchase_unit": stock.get("purchase_unit", "包"),
            "target_date": target_date, "forecast_usage": _number(usage[iid]),
            "available": _number(row["available"]), "physical_on_hand": _number(row["physical"]),
            "excluded_stock": _number(row["excluded"]), "due_inbound": _number(row["due"]),
            "late_inbound": _number(row["late"]), "safety_stock": _number(safety),
            "shortage": _number(shortage), "suggested_quantity": _number(order),
            "pack_size": _number(pack), "min_order": _number(minimum),
            "max_order": stock["max_order"], "storage_capacity": stock["storage_capacity"],
            "supplier_id": stock["supplier_id"], "supplier_name": supplier["name"],
            "delivery_date": supplier["delivery_date"], "email": supplier["email"],
            "whatsapp": supplier["whatsapp"], "approval_warnings": [],
        }
        item["blockers"] = _constraints(item, order)
        blockers.extend(item["blockers"])
        items.append(item)
    return {
        "shop_id": inventory["shop_id"], "as_of_date": inventory["as_of_date"],
        "snapshot_id": inventory["snapshot_id"], "target_date": target_date,
        "data_source": "synthetic", "forecast_total_units": _number(total_units),
        "items": items, "blockers": blockers, "warnings": warnings,
        "stock_semantics": "收盘批次已扣今天销量；在途仅含未收货且开店前可用量；采购批准不改变库存。",
        "formula": "shortage=max(0,forecast_usage+safety_stock-available-due_inbound); order=ceil(max(shortage,min_order)/pack_size)*pack_size if shortage>0 else 0",
    }


def validate_quantities(proposal, overrides):
    """Validate owner edits and return copied lines carrying approval warnings.

    An explicit zero skips a line. Reducing below shortage is allowed only as an
    owner decision recorded in approval_warnings; it does not erase the shortage.
    Invalid package multiples, nonzero MOQ, storage, order or lead-time limits
    raise CafeError. Editing may resolve the original recommendation's blockers.
    """
    if not isinstance(proposal, dict) or not isinstance(proposal.get("items"), list):
        raise CafeError("采购建议格式不正确。")
    if not isinstance(overrides, dict):
        raise CafeError("修改数量必须是 ingredient_id 到数字的对象。")
    ids = {item["ingredient_id"] for item in proposal["items"]}
    unknown = overrides.keys() - ids
    if unknown:
        raise CafeError(f"修改包含未知食材：{sorted(unknown)}。")
    result = deepcopy(proposal["items"])
    for item in result:
        iid = item["ingredient_id"]
        amount = _quantity(overrides.get(iid, item["suggested_quantity"]), f"{item['name']}采购量", unit=item["unit"])
        pack = _quantity(item["pack_size"], "pack_size", positive=True)
        minimum = _quantity(item["min_order"], "min_order")
        if amount and (amount % pack):
            raise CafeError(f"{item['name']}采购量必须为每包 {_number(pack)} {item['unit']} 的整数倍。")
        if amount and amount < minimum:
            raise CafeError(f"{item['name']}非零采购量不能低于最小起订量 {_number(minimum)} {item['unit']}。")
        problems = _constraints(item, amount)
        if problems:
            raise CafeError("；".join(problems))
        item["approved_quantity"] = _number(amount)
        item["approval_warnings"] = []
        shortage = _quantity(item["shortage"], "shortage")
        if amount < shortage:
            item["approval_warnings"].append(f"店主选择的数量低于缺口，仍有 {_number(shortage - amount)} {item['unit']} 未覆盖；可能缺货。")
        item["blockers"] = []
    if not any(item["approved_quantity"] > 0 for item in result):
        raise CafeError("全部采购量为 0；无需生成空采购单，请改为拒绝/跳过建议。")
    return result
