"""Build the public demo's immutable BOM from the two original CSV files.

Quantities remain in CSV base units (g/ml/individual item). Initial stocks are
explicit demonstration assumptions; safety thresholds are never inferred.
"""
import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_SAFE = 2**53 - 1
BASE = {
    "拼配豆": "INV-001", "全脂奶": "INV-003", "脱脂奶": "INV-004",
    "燕麦奶": "INV-005", "巧克力酱": "INV-010", "茶": "INV-012",
}
# The generic syrup/puree/liquid CSV columns require this explicit product join.
SPECIAL = {
    ("BV-006", "糖浆"): "INV-007", ("BV-010", "糖浆"): "INV-008",
    ("BV-020", "糖浆"): "INV-009", ("BV-016", "果泥/酱"): "INV-018",
    ("BV-022", "果泥/酱"): "INV-015", ("BV-023", "果泥/酱"): "INV-016",
    ("BV-015", "其他液体"): "INV-021", ("BV-020", "其他液体"): "INV-019",
    ("BV-023", "其他液体"): "INV-021",
}
# Water/ice units and stock are supplementary demo assumptions. The supplied
# safety CSV contains no thresholds for these, so safety stays explicitly null.
AUX = {
    "冰水": ("AUX-COLD", "ml", 20000), "热水": ("AUX-HOT", "ml", 20000),
    "冰块": ("AUX-ICE", "g", 10000),
}
FIELDS = [
    "拼配豆", "SOE豆", "全脂奶", "脱脂奶", "燕麦奶", "糖浆", "巧克力酱",
    "抹茶粉", "茶", "果泥/酱", "其他液体", "热水", "冰水", "冰块", "温度供应",
]
BOM_HEADERS = ["饮品ID", "饮品名称"] + [
    size + "·" + field for field in FIELDS for size in ("中杯", "大杯")
] + ["成品(个)"]
SAFETY_HEADERS = ["原料ID", "原料名称", "单位", "安全线"]


def rows(path, expected_headers):
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or set(headers) != set(expected_headers):
            raise ValueError(f"{Path(path).name}字段与原始CSV结构不一致")
        result = list(reader)
    if not result:
        raise ValueError(f"{Path(path).name}没有数据")
    for row in result:
        if None in row or any(value is None or not value.strip() for value in row.values()):
            raise ValueError(f"{Path(path).name}存在空值或列数不一致")
    return result


def amount(value):
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"无效数量：{value}") from exc
    if (
        not number.is_finite() or number < 0
        or number != number.to_integral_value() or number > MAX_SAFE
    ):
        raise ValueError(f"本表使用非负整数基础单位，发现无效数量：{value}")
    return int(number)


def build(bom_path, safety_path):
    materials, products = {}, {}
    for row in rows(safety_path, SAFETY_HEADERS):
        key, safety = row["原料ID"], amount(row["安全线"])
        if key in materials:
            raise ValueError(f"重复原料ID：{key}")
        if row["单位"] not in {"g", "ml", "个"}:
            raise ValueError(f"不支持的基础单位：{row['单位']}")
        # CSV milk safety = 51200 ml; oat safety = 2800 ml.
        initial = {"INV-003": 60000, "INV-005": 8000}.get(key, safety * 2)
        if initial > MAX_SAFE:
            raise ValueError(f"演示库存超出安全整数范围：{key}")
        materials[key] = dict(
            name=row["原料名称"], unit=row["单位"], safety=safety, initial=initial,
        )

    for row in rows(bom_path, BOM_HEADERS):
        key, recipes = row["饮品ID"], {}
        if key in products:
            raise ValueError(f"重复产品ID：{key}")
        finished = amount(row["成品(个)"])
        for size in ("中杯", "大杯"):
            recipe = {}
            for field in FIELDS:
                if field == "温度供应":
                    continue
                quantity = amount(row[size + "·" + field])
                if quantity == 0:
                    continue
                material = SPECIAL.get((key, field), BASE.get(field))
                if field in AUX:
                    material, unit, initial = AUX[field]
                    materials.setdefault(
                        material,
                        dict(name=field, unit=unit, safety=None, initial=initial),
                    )
                if material not in materials:
                    raise ValueError(f"非零配料缺少原料映射或安全线配置：{key}/{size}·{field}")
                recipe[material] = recipe.get(material, 0) + quantity
                if recipe[material] > MAX_SAFE:
                    raise ValueError(f"配方数量超出范围：{key}/{material}")
            recipes[size] = recipe

        if finished:
            if key not in materials or materials[key]["unit"] != "个" or any(recipes.values()):
                raise ValueError(f"成品配置冲突：{key}")
            recipes = {"份": {key: finished}}
        elif not all(recipes.values()):
            raise ValueError(f"饮品配方为空：{key}")
        products[key] = dict(name=row["饮品名称"], recipes=recipes)

    data = dict(materials=materials, products=products)
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True)
    data["version"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return data


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    data = build(root / "data/配料表.csv", root / "data/安全线.csv")
    target = root / "dist/demo/bom-data.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"BOM: {len(data['products'])} products, {len(data['materials'])} materials -> {target}")
