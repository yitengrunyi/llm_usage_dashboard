"""road2all adapter — x.road2all.com.

上游 2026-08 起 invoice 行带 usage 拆分: cache_read/cache_write 都有值
(此前上游不暴露, 一直是 NULL). 客户端 prompt_tokens 已是"完整 input 含 cache"口径.

fetch_apikey_rows: invoice 行自带 account (上游子账号名, 控制台一账号一把 key),
是这接口唯一的 key 粒度 — 同一份行按 (account, model) 再聚合, 不额外发请求.
"""
from __future__ import annotations

import datetime as dt

from road2all_client import aggregate_logs, aggregate_logs_by_account, fetch_logs
from utils import normalize_model_name
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_total


class Road2AllAdapter(VendorAdapter):
    vendor_id = "road2all"
    source = "road2all_invoice"
    native_currency = "USD"
    has_cache_detail = True  # usage.cacheRead / cacheWrite(5m+1h)

    # job._ingest_one_day 同一天先调 fetch_one_day 再调 fetch_apikey_rows (同一实例),
    # memo 当天原始 invoice 行避免同一天拉两次 (单 vendor 按天串行, 无并发竞争)
    _rows_day: dt.date | None = None
    _rows_cache: list | None = None

    def _logs_for_day(self, day: dt.date) -> list:
        if self._rows_cache is None or self._rows_day != day:
            start_iso = f"{day.isoformat()}T00:00:00+08:00"
            end_iso = f"{day.isoformat()}T23:59:59+08:00"
            self._rows_cache = fetch_logs(self.vendor, start_iso, end_iso)
            self._rows_day = day
        return self._rows_cache

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        result = aggregate_logs(self._logs_for_day(day))
        # 同一 norm-name 多 raw (e.g. claude-sonnet-4-20250514 + claude-sonnet-4-20250515)
        # 在客户端这一步先归一+合并, 不丢 token / cost.
        merged: dict[str, dict] = {}
        for model_name, m in (result.get("models") or {}).items():
            key = normalize_model_name(model_name) or model_name
            agg = merged.setdefault(key, {
                "prompt_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "total_count": 0, "total_cost": 0.0,
            })
            agg["prompt_tokens"] += int(m.get("prompt_tokens") or 0)
            agg["completion_tokens"] += int(m.get("completion_tokens") or 0)
            agg["cache_read_tokens"] += int(m.get("cache_read_tokens") or 0)
            agg["cache_write_tokens"] += int(m.get("cache_write_tokens") or 0)
            agg["total_count"] += int(m.get("total_count") or 0)
            agg["total_cost"] += float(m.get("total_cost") or 0)

        rows: list[ModelRow] = []
        for model_name, m in merged.items():
            prompt_full = m["prompt_tokens"]  # 客户端已含 cache
            comp = m["completion_tokens"]
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=m["cache_read_tokens"],
                cache_write_tokens=m["cache_write_tokens"],
                total_tokens=pack_total(prompt_full, comp),
                request_count=m["total_count"],
                image_count=None,
                cost_native=m["total_cost"],
            ))
        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (account, norm model) 拆分 — vendor 详情页 "按 API key 筛选" 用.

        与 fetch_one_day 同一份 invoice 行 (memo), model 归一口径也一致;
        account 缺失的行在 client 侧已被跳过.
        """
        by_account = aggregate_logs_by_account(self._logs_for_day(day))
        merged: dict[tuple[str, str], dict] = {}
        for account, models in by_account.items():
            for model_name, m in models.items():
                key = normalize_model_name(model_name) or model_name
                agg = merged.setdefault((account, key), {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "total_count": 0, "total_cost": 0.0,
                })
                agg["prompt_tokens"] += int(m.get("prompt_tokens") or 0)
                agg["completion_tokens"] += int(m.get("completion_tokens") or 0)
                agg["cache_read_tokens"] += int(m.get("cache_read_tokens") or 0)
                agg["cache_write_tokens"] += int(m.get("cache_write_tokens") or 0)
                agg["total_count"] += int(m.get("total_count") or 0)
                agg["total_cost"] += float(m.get("total_cost") or 0)

        rows: list[ApiKeyRow] = []
        for (account, model_name), m in merged.items():
            prompt_full = m["prompt_tokens"]  # 客户端已含 cache
            comp = m["completion_tokens"]
            rows.append(ApiKeyRow(
                api_key=account,
                model=model_name,
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=m["cache_read_tokens"],
                cache_write_tokens=m["cache_write_tokens"],
                total_tokens=pack_total(prompt_full, comp),
                request_count=m["total_count"],
                image_count=None,
                cost_native=m["total_cost"],
            ))
        return rows
