"""wangsu adapter — 网宿 AIGW.

主表: wangsu_client.fetch_vendor_usage 内部已经按日聚合, 直接传 day 取一天即可.
上游字段全 (prompt/completion/cache_read/cache_write 都有).

API key 拆分: open API 没有 key 维度, 走控制台接口 (wangsu_console_client,
Cookie 会话) 的 tokenName×modelName 交叉维度. 金额是控制台真实账单 (比主表
定价反推准), 按模型分币种, 统一折成 CNY (adapter native) 后按 (key, model)
合并入库. Cookie 失效 → 记 warning 返空, 不阻塞主表入库 (key 表是辅助数据).
"""
from __future__ import annotations

import datetime as dt
import logging

from exchange_rate import get_rates
from wangsu_client import fetch_vendor_usage
from wangsu_console_client import fetch_token_day_rows
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total

log = logging.getLogger("ingest.adapters.wangsu")


class WangsuAdapter(VendorAdapter):
    vendor_id = "wangsu"
    source = "wangsu_aigw_billing"
    native_currency = "CNY"
    has_cache_detail = True

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"
        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            cr = m.get("cache_read_tokens")
            cw = m.get("cache_write_tokens")
            prompt_full = pack_input(m.get("prompt_tokens"), cr, cw)
            comp = m.get("completion_tokens")
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                total_tokens=pack_total(prompt_full, comp),
                request_count=m.get("total_count"),
                image_count=m.get("image_count"),
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (令牌, model) 拆一天用量, 写 vendor_apikey_usage_daily.

        控制台 prompt_tokens 已是完整输入 (含 cache), 直接用不再 pack_input.
        同 (key, model) 多行 (币种拆分) 先折 CNY 再合并 — native_currency=CNY,
        job.py 的 _convert_cost 才能把 usd/cny 算对.
        """
        try:
            raw = fetch_token_day_rows(self.vendor, day)
        except Exception as e:
            log.warning("[wangsu] %s 令牌维度拉取失败 (key 表不动): %s", day, e)
            return []
        if not raw:
            return []

        fx = float(get_rates().get("CNY") or 7.2)
        merged: dict[tuple[str, str], dict] = {}
        for r in raw:
            cny = r["amount"] if r["currency"] == "CNY" else r["amount"] * fx
            k = (r["token_name"], r["model"])
            g = merged.setdefault(k, {
                "cny": 0.0, "prompt": 0, "completion": 0,
                "cache_r": 0, "cache_w": 0, "visit": 0,
            })
            g["cny"] += cny
            g["prompt"] += r["prompt_tokens"]
            g["completion"] += r["completion_tokens"]
            g["cache_r"] += r["cache_read"]
            g["cache_w"] += r["cache_write"]
            g["visit"] += r["visit"]

        return [
            ApiKeyRow(
                api_key=key,
                model=model,
                prompt_tokens=g["prompt"] or None,
                completion_tokens=g["completion"] or None,
                cache_read_tokens=g["cache_r"],
                cache_write_tokens=g["cache_w"],
                total_tokens=(g["prompt"] + g["completion"]) or None,
                request_count=g["visit"],
                image_count=None,
                cost_native=round(g["cny"], 6),
            )
            for (key, model), g in merged.items()
        ]
