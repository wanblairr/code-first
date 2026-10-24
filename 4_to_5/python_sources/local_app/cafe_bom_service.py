"""Authenticated Python facade for the public-equivalent CSV BOM assistant.

The database owns inventory, proposals and approvals. Context reads do not call
providers, train a model, or overwrite old input/inventory files.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path

from cafe_core import CafeError, fingerprint, save_json
from cafe_bom import BOMRepository, load_bom_data, low_stock, forecast_menu
from cafe_bom_context import forecast_context
from cafe_bom_pricing import BOMPricingRepository

SOURCE = Path(__file__).resolve().parent
MODES = {"source", "rain", "sunny", "mild"}
HOLIDAYS = {"auto", "public_holiday", "ordinary"}


class BOMApplication:
    def __init__(self, service):
        self.service = service
        self.data = load_bom_data()
        self.repo = BOMRepository(service.store, self.data)
        self.pricing = BOMPricingRepository(service.store, self.data)
        self.settings_path = service.runtime / "bom_context_settings.json"
        self.model = json.loads((SOURCE / "data/bom-context-model.json").read_text(encoding="utf-8"))

    def settings(self):
        if self.settings_path.exists():
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if value.get("weather_mode") not in MODES or value.get("holiday_mode") not in HOLIDAYS:
                raise CafeError("BOM天气／假日设置无效，请重新选择情景。")
            return value
        return {"weather_mode": "source", "holiday_mode": "auto", "known_at": None}

    def context(self):
        from cafe_service import SHANGHAI, load_json
        from cafe_context import holiday_for_day
        from cafe_context_workflow import valid_context
        settings = self.settings()
        instant = self.service.clock().astimezone(SHANGHAI)
        target = (instant.date() + timedelta(days=1)).isoformat()
        mode = settings["weather_mode"]
        holiday = holiday_for_day(target)
        if settings["holiday_mode"] == "auto":
            if holiday.get("is_holiday") is None or not holiday.get("verified"):
                raise CafeError("该日期的吉隆坡假日状态尚未核验，请明确选择模拟假日情景。")
            is_holiday = holiday["is_holiday"]
        else:
            is_holiday = settings["holiday_mode"] == "public_holiday"
        if mode == "source":
            path = self.service.data_dir / "target_context.json"
            if not path.exists():
                raise CafeError("请先准备天气资料，或在助手设置中选择明确的模拟天气。")
            snapshot = load_json(path)
            # The original snapshot remains immutable; holiday overrides apply only
            # to this explicitly labelled BOM demonstration.
            valid_context(self.service, snapshot, target)
            temperature, rain = snapshot["temperature_c"], snapshot["precipitation_mm"]
            known_at = snapshot["known_at"]
            weather_kind = snapshot["weather_kind"]
            source = snapshot.get("source", "existing demo context")
        else:
            temperature, rain = {"rain": (27, 24), "sunny": (32, 0), "mild": (29, 6)}[mode]
            known_at = settings.get("known_at") or instant.isoformat()
            weather_kind, source = "synthetic_forecast", "explicit_bom_simulation"
        snapshot = {
            "date": target, "temperature_c": temperature, "precipitation_mm": rain,
            "is_holiday": is_holiday, "is_workday_override": False,
            "known_at": known_at, "weather_kind": weather_kind, "source": source,
            "holiday_name": holiday.get("holiday_name", "") if settings["holiday_mode"] == "auto"
                else ("公共假日 · 模拟情景" if is_holiday else "非假日 · 模拟情景"),
            "holiday_source": holiday.get("source", ""),
            "holiday_notes": holiday.get("notes", []),
            "holiday_verified": settings["holiday_mode"] == "auto" and holiday.get("verified", False),
            "scenario": mode != "source" or settings["holiday_mode"] != "auto",
        }
        context = forecast_context(self.model, target, temperature, rain, is_holiday,
            signature_material={"weather_kind": weather_kind, "source": source,
                "known_at": known_at, "weather_mode": mode, "holiday_mode": settings["holiday_mode"]})
        context["snapshot"] = snapshot
        context["asOfDate"] = instant.date().isoformat()
        return context

    def state(self):
        with self.service.lock:
            error, context = None, None
            try:
                context = self.context()
            except (CafeError, OSError, ValueError, KeyError) as exc:
                error = str(exc)
            result = self.repo.snapshot(context)
            from cafe_service import SHANGHAI
            from cafe_sales_learning import build_popularity
            result["as_of_date"] = self.service.clock().astimezone(SHANGHAI).date().isoformat()
            result["popularity"] = build_popularity(self.data, result["sales_history"], result["as_of_date"])
            result.update(data=deepcopy(self.data), context_ready=context is not None,
                context_error=error, bom_context_settings=self.settings(),
                context_snapshot=deepcopy(context["snapshot"]) if context else None,
                low_stock=low_stock(self.data, result["stock"]))
            pricebook = result.pop("pricebook", None) or self.pricing.state()
            result["pricing"] = {"revision": pricebook["price_revision"], "currency": "MYR",
                "entries": [dict(quote, ingredient_id=key) for key, quote in pricebook["prices"].items() if quote is not None]}
            current_hash = result["current_input_hash"]
            result["proposal"] = next((p for p in result["proposals"]
                if p["input_hash"] == current_hash and p["status"] in ("pending", "approved")), None)
            drafts = result.pop("drafts")
            for order in result["orders"]:
                order["drafts"] = [{key: value for key, value in draft.items() if key != "email_eml"}
                    for draft in drafts if draft["order_id"] == order["id"]]
            for event in result["events"]:
                event["payload"] = json.loads(event["detail"])
                if event["action"] in ("bom_sale_recorded", "bom_sale_undone"):
                    event["request_id"] = event["subject"]
            if context:
                result["forecast_context"] = {key: deepcopy(context[key]) for key in
                    ("targetDate", "forecastCups", "referenceCups", "forecasts")}
            with self.service.store.connect() as db:
                exports = []
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_drafts'").fetchone():
                    exports = [json.loads(row[0]) for row in db.execute("SELECT document FROM external_drafts ORDER BY rowid DESC LIMIT 50")]
                sandbox = []
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsapp_sandbox_messages'").fetchone():
                    sandbox = [json.loads(row[0]) for row in db.execute("SELECT document FROM whatsapp_sandbox_messages ORDER BY rowid DESC LIMIT 30")]
                result["provider_status"] = {"gmail_exports": [{key: record.get(key) for key in ("draft_id", "status", "gmail_draft_id")} for record in exports],
                    "sandbox_messages": [{key: record.get(key) for key in ("draft_id", "status", "message_id")} for record in sandbox]}
            result["context_panel"] = self.local_context_panel(result)
            return result

    def local_context_panel(self, state=None):
        state = state or self.state()
        if not state["context_ready"]:
            return {"enabled": True, "status": "needs_refresh", "error": state["context_error"],
                "snapshot": {"date": (self.service.clock().date()+timedelta(days=1)).isoformat(),
                    "temperature_c": None, "precipitation_mm": None, "is_holiday": None,
                    "weather_kind": "unavailable", "known_at": None},
                "analysis_current": False, "analysis": None}
        c = state["forecast_context"]
        items = [{"sku": f["sku"], "baseline_units": f["referenceCups"],
            "combined_units": f["forecastCups"], "weather_delta_units": f["weatherEffect"],
            "holiday_delta_units": f["holidayEffect"]} for f in c["forecasts"]]
        return {"enabled": True, "status": "ready", "error": None, "snapshot": state["context_snapshot"],
            "analysis_current": True, "analysis": {
                "context": state["context_snapshot"], "impact": {"items": items}, "ingredients": [], "bom_menu": True,
                "reference_description": ("已录入完整日销量采用7天半衰期加权均值 × 天气／假日系数；不同饮品与烘焙分别排行。这是统计估计，样本不足7天时可信度低。"
                    if state.get("sales_history", {}).get("days") else
                    "尚无录入销量，与公开版使用相同合成模型及明确模拟菜单配比 × 情景系数，未训练20SKU真实销量。")}}

    def set_context(self, values):
        if not isinstance(values, dict) or set(values) - {"weather_mode", "holiday_mode"}:
            raise CafeError("天气设置字段无效。")
        settings = self.settings()
        for key, allowed in (("weather_mode", MODES), ("holiday_mode", HOLIDAYS)):
            value = values.get(key, settings[key])
            if not isinstance(value, str) or value not in allowed:
                raise CafeError("天气或假日情景无效。")
            settings[key] = value
        with self.service.lock:
            from cafe_service import SHANGHAI
            settings["known_at"] = self.service.clock().astimezone(SHANGHAI).isoformat()
            save_json(self.settings_path, settings)
            result = self.state()
            self.service.store.log("bom_scenario_selected",
                {"weather_mode": settings["weather_mode"], "holiday_mode": settings["holiday_mode"]},
                "bom", actor="owner")
            return result

    def use_source(self):
        return self.set_context({"weather_mode": "source", "holiday_mode": "auto"})

    def sale(self, values):
        with self.service.lock:
            sale = {key: values[key] for key in ("productId", "size", "quantity")}
            result = self.repo.simulate_sale(sale, values["revision"], values["request_id"])
            state = self.state()  # An idempotent old response must not restore old stock.
            state["sale_result"] = result
            return state

    def undo(self, values):
        with self.service.lock:
            outcome = self.repo.undo_sale(values["revision"], values["request_id"], values["transaction_id"])
            state = self.state()
            state["undo_result"] = outcome
            return state

    def set_prices(self, values):
        if not isinstance(values, dict) or set(values) - {"revision", "entries", "request_id"}:
            raise CafeError("采购价格设置字段无效。")
        with self.service.lock:
            entries = values["entries"]
            if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
                raise CafeError("采购价格必须为报价数组。")
            entries = [dict(row, currency=row.get("currency", "MYR"),
                source=row.get("source", "unquoted" if row.get("pack_quantity") is None and row.get("pack_price_cents") is None else "owner_entry")) for row in entries]
            self.pricing.set_prices(entries, values["revision"])
            return self.state()

    def set_history(self, values):
        if not isinstance(values, dict) or set(values) - {"revision", "date", "rows", "day", "request_id"}:
            raise CafeError("销量录入字段无效。")
        if "day" in values and ("date" in values or "rows" in values):
            raise CafeError("请仅使用一种销量日期格式。")
        request_id = values.get("request_id", "")
        if not isinstance(request_id, str) or len(request_id) > 200:
            raise CafeError("销量请求编号无效。")
        from cafe_service import SHANGHAI
        with self.service.lock:
            as_of_date = self.service.clock().astimezone(SHANGHAI).date().isoformat()
            self.repo.learning.save_history(values.get("day", {"date": values.get("date"), "rows": values.get("rows")}),
                values.get("revision"), as_of_date, request_id)
            state = self.state()
            state["history_result"] = {"request_id": request_id, "revision": state["sales_history"]["revision"]}
            return state

    def set_extras(self, values):
        if not isinstance(values, dict) or set(values) - {"revision", "items", "request_id"}:
            raise CafeError("加购录入字段无效。")
        request_id = values.get("request_id", "")
        if not isinstance(request_id, str) or len(request_id) > 200:
            raise CafeError("加购请求编号无效。")
        with self.service.lock:
            self.repo.learning.save_extras(values.get("items"), values.get("revision"), request_id)
            state = self.state()
            state["extras_result"] = {"request_id": request_id, "revision": state["extras"]["revision"]}
            return state

    def suggest(self):
        with self.service.lock:
            self.repo.propose(self.context())
            return self.state()

    def preview(self, values):
        with self.service.lock:
            return self.repo.preview(values["proposal_id"], values["revision"], values["input_hash"],
                values.get("quantities", {}), self.context())

    def approve(self, values):
        with self.service.lock:
            self.repo.approve(values["proposal_id"], values["revision"], values["input_hash"],
                values.get("quantities", {}), self.context(), values.get("acknowledged", False))
            return self.state()

    def draft(self, draft_id):
        return self.repo.draft(draft_id)

    def approved_draft(self, draft_id):
        with self.service.lock:
            return self.repo.approved_draft(draft_id, self.context())
