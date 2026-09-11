"""xhub adapter — new-api-direct 系 (账号密码登录).

走快路径 /api/data/self 拿 total/req/cost (cache 拆分 NULL).
慢路径补字段在 [ingest/newapi_direct_slow.py](ingest/newapi_direct_slow.py).

为啥分两阶段: raw log /api/log/self 上游按 LRU 滚动清理 (xhub 高 throughput 账号只剩末尾几十分钟),
backfill 跑到老天就拉不全, 历史数据永久残缺. data/self 是预聚合表保留更久 + 总量准.
trade-off: 字段不全 vs 能查得到 — 跟 blueshirt 同款选择.
"""
from __future__ import annotations

import datetime as dt

from newapi_direct_client import fetch_vendor_usage
from ingest.adapters.base import ModelRow, VendorAdapter


class _NewApiDirectAdapter(VendorAdapter):
    """new-api-direct 公共基类 (当前只有 xhub/nulls, 保留基类方便再加同类 vendor)."""
    source = "newapi_data_self"
    native_currency = "USD"
    has_cache_detail = False  # data/self 不给 cache 拆分, 慢路径补

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"

        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=None,             # data/self 不给拆分
                completion_tokens=None,
                cache_read_tokens=None,
                cache_write_tokens=None,
                total_tokens=m.get("total_tokens"),
                request_count=m.get("total_count"),
                image_count=None,
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows


    def fetch_upstream_total_cost(self, day: dt.date) -> float | None:
        """复用 fetch_vendor_usage (走快路径 /api/data/self), 直接读 total_cost.
        快路径本身就是 server 端预聚合, 不会比 fetch_one_day 慢; 但拿到的是同一份数据,
        所以 0 / >0 跟 fetch_one_day 一致, 真 0 / 真有钱都信得过.
        """
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"
        try:
            result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
            return float(result.get("total_cost") or 0)
        except Exception:
            return None


class XhubAdapter(_NewApiDirectAdapter):
    vendor_id = "nulls"
