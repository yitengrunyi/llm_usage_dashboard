"""ucloud adapter — UModelVerse.

字段策略 (双源):
- 主源 ListPaidOrders: prompt/completion/cache_read/cache_write (从 PricingSKU 解析)
                       + cost (订单金额)
- 辅源 GetUMInferTokenUsage: request_count (调用次数)
- 两源用 normalize_model_name 合并 (e.g. 'claude-opus-4-6-200k' → 'claude-opus-4-6')

API key 拆分: ListPaidOrders 每单带 ResourceID/ResourceName (即 UCloud 的 API key),
fetch_apikey_rows 按 (key, model) 聚合写 vendor_apikey_usage_daily; per-key 调用次数
用 GetUMInferTokenUsage(KeyId) 拿 key 级 RequestTotal, 按各模型 out-token 占比分摊.
"""
from __future__ import annotations

import datetime as dt

from ucloud_client import (
    _client, _query_token_usage, fetch_orders_for_day, fetch_orders_for_day_by_key,
    fetch_vendor_usage,
)
from utils import normalize_model_name
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total


class UCloudAdapter(VendorAdapter):
    vendor_id = "ucloud"
    source = "ucloud_orders+token_usage"
    native_currency = "CNY"
    has_cache_detail = True

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        # 1. orders 拿 token 细分 + cache + cost
        orders = fetch_orders_for_day(self.vendor, day)
        orders_norm: dict[str, dict] = {}
        for mname, m in orders.items():
            key = normalize_model_name(mname) or mname
            if key in orders_norm:
                for k, v in m.items():
                    orders_norm[key][k] = orders_norm[key].get(k, 0) + v
            else:
                orders_norm[key] = dict(m)

        # 2. token_usage 拿调用次数
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"
        usage_result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        request_counts: dict[str, int] = {}
        for mname, m in (usage_result.get("models") or {}).items():
            key = normalize_model_name(mname) or mname
            request_counts[key] = request_counts.get(key, 0) + int(m.get("total_count") or 0)

        # 3. 合并 — normalize 后 model 名一致
        all_models = set(orders_norm) | set(request_counts)
        rows: list[ModelRow] = []
        for model_name in all_models:
            o = orders_norm.get(model_name, {})
            rc = request_counts.get(model_name)
            prompt = o.get("prompt_tokens", 0)
            completion = o.get("completion_tokens", 0)
            cache_r = o.get("cache_read_tokens", 0)
            cache_w = o.get("cache_write_tokens", 0)
            prompt_full = pack_input(prompt, cache_r, cache_w)
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=prompt_full or None,
                completion_tokens=completion or None,
                cache_read_tokens=cache_r,
                cache_write_tokens=cache_w,
                total_tokens=pack_total(prompt_full, completion) or None,
                request_count=rc,
                image_count=None,
                cost_native=float(o.get("total_cost", 0)),
            ))
        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (API key, model) 拆分一天订单, 写 vendor_apikey_usage_daily.

        api_key 用 ResourceName (用户起的 key 名, e.g. '魏召'); 上游没给名字时退 ResourceID.
        request_count: GetUMInferTokenUsage 按 KeyId 拿 key 级 RequestTotal (准),
        分摊到模型按各自 out-token 占比 (估算 — 跟 fetch_vendor_usage 同一口径).
        """
        by_key = fetch_orders_for_day_by_key(self.vendor, day)
        if not by_key:
            return []

        start_ts = int(dt.datetime(day.year, day.month, day.day, 0, 0, 0).timestamp())
        end_ts = int(dt.datetime(day.year, day.month, day.day, 23, 59, 59).timestamp())
        client = _client(self.vendor)
        project_id = self.vendor.get("project_id", "")

        rows: list[ApiKeyRow] = []
        for rid, entry in by_key.items():
            # 1. 订单按 (key, model) 聚合, model 名归一化合并
            models: dict[str, dict] = {}
            for mname, m in entry["models"].items():
                key = normalize_model_name(mname) or mname
                agg = models.setdefault(key, {
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "total_cost": 0.0,
                })
                agg["prompt_tokens"] += m["prompt_tokens"]
                agg["completion_tokens"] += m["completion_tokens"]
                agg["cache_read_tokens"] += m["cache_read_tokens"]
                agg["cache_write_tokens"] += m["cache_write_tokens"]
                agg["total_cost"] += m["total_cost"]

            # 2. key 级调用次数 → 按各模型 completion 占比分摊
            usage = _query_token_usage(client, project_id, rid, start_ts, end_ts)
            total_calls = int(usage.get("RequestTotal") or 0) if usage else None
            out_sum = sum(m["completion_tokens"] for m in models.values())
            for key, m in models.items():
                if total_calls and out_sum > 0:
                    rc = int(round(total_calls * m["completion_tokens"] / out_sum))
                else:
                    rc = None  # 拉不到 usage 或当天没 output → 上游无数据语义
                prompt = m["prompt_tokens"]
                cache_r = m["cache_read_tokens"]
                cache_w = m["cache_write_tokens"]
                completion = m["completion_tokens"]
                prompt_full = pack_input(prompt, cache_r, cache_w)
                rows.append(ApiKeyRow(
                    api_key=entry["name"] or rid,
                    model=key,
                    prompt_tokens=prompt_full or None,
                    completion_tokens=completion or None,
                    cache_read_tokens=cache_r,
                    cache_write_tokens=cache_w,
                    total_tokens=pack_total(prompt_full, completion) or None,
                    request_count=rc,
                    image_count=None,
                    cost_native=float(m["total_cost"]),
                ))
        return rows
