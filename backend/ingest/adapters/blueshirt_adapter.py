"""blueshirt adapter (vendor_id=gptmeta).

走 `/api/data/self` 预聚合接口 — 1 秒/天.

字段限制 (raw log 撑不住, 大账号一天 14.8 万条 log, 翻 1485 页直接卡死):
- prompt_tokens / completion_tokens: 不拆分, 留 None (上游只给 token_used 合计)
- cache_read / cache_write: NULL (data/self 不给)
- total_tokens / total_count / cost_native: ✓

trade-off: 字段不全 vs 能查得到. 现实只能选后者.
"""
from __future__ import annotations

import datetime as dt

from newapi_client import fetch_vendor_usage
from ingest.adapters.base import ModelRow, VendorAdapter


class BlueshirtAdapter(VendorAdapter):
    vendor_id = "blueshirt"
    source = "newapi_data_self"
    native_currency = "USD"
    has_cache_detail = False  # data/self 不给 cache 拆分

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"

        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            total = m.get("total_tokens")
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=None,             # data/self 不给拆分
                completion_tokens=None,
                cache_read_tokens=None,
                cache_write_tokens=None,
                total_tokens=total,
                request_count=m.get("total_count"),
                image_count=None,
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows
