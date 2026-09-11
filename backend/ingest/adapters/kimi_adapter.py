"""kimi adapter — Moonshot/Kimi 平台.

kimi_client.fetch_vendor_usage 内部已经按天聚合, 直接传 day 取一天.
上游无 cache_write 概念 (auto-caching 全归 cache_read).

fetch_apikey_rows: consumes 行带 api_key_name, 按 (api_key, model) 拆分写 key 表.
跟 apevon 一样, 主表行为不变.
"""
from __future__ import annotations

import datetime as dt

from kimi_client import fetch_vendor_usage, _fetch_raw, _state_file, _iso_to_ms, aggregate_by_apikey
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total


class KimiAdapter(VendorAdapter):
    vendor_id = "kimi"
    source = "kimi_consumes"
    native_currency = "CNY"
    has_cache_detail = True  # 有 cache_read, cache_write 永远 0

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"
        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            cr = m.get("cache_read_tokens")
            prompt_full = pack_input(m.get("prompt_tokens"), cr, None)
            comp = m.get("completion_tokens")
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=cr,
                cache_write_tokens=None,  # kimi 上游就没这概念, 不伪造 0
                total_tokens=pack_total(prompt_full, comp),
                request_count=None,  # kimi consumes 不返调用次数
                image_count=None,
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (api_key_name, model) 拆分 consumes, 写 vendor_apikey_usage_daily.

        复用 kimi_client 的原始行拉取 + aggregate_by_apikey 反推 token.
        上游 consumes 偶尔返空 → 返空 list.
        """
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"
        state_path = _state_file(self.vendor["base_url"])
        if not state_path.exists():
            return []
        try:
            start_ms = _iso_to_ms(start_iso)
            end_ms = _iso_to_ms(end_iso)
            raw_rows = _fetch_raw(self.vendor["base_url"], state_path, start_ms, end_ms)
        except Exception:
            return []

        groups = aggregate_by_apikey(raw_rows)
        out: list[ApiKeyRow] = []
        for g in groups:
            cr = g["cache_read_tokens"]
            prompt_full = pack_input(g["prompt_tokens"], cr, None)
            comp = g["completion_tokens"]
            out.append(ApiKeyRow(
                api_key=g["api_key"],
                model=g["model"],
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=cr,
                cache_write_tokens=None,
                total_tokens=pack_total(prompt_full, comp),
                request_count=None,
                image_count=None,
                cost_native=g["total_cost"],
            ))
        return out
