"""
智谱 BigModel adapter — bigmodel.cn

字段完整度:
- ✅ prompt_tokens (输入)
- ✅ completion_tokens (输出)
- ✅ cache_read_tokens (缓存命中)
- ✅ cache_write_tokens (缓存写入)
- ✅ request_count (apiUsage)
- ✅ cost (originalAmount)

数据来源: /api/finance/expenseBill/expenseBillListByDay (按天明细)
认证方式: Playwright 微信扫码登录 → JWT Token + Cookie
"""
from __future__ import annotations

import datetime as dt

from bigmodel_client import (
    _get_state_file,
    _load_session,
    fetch_bills_by_day,
    fetch_vendor_usage,
)
from utils import normalize_model_name
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total


class BigModelAdapter(VendorAdapter):
    vendor_id = "bigmodel"
    source = "bigmodel_expense_bill_daily"
    native_currency = "CNY"
    has_cache_detail = True  # 支持 cache 拆分！

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        """
        按天拉取数据。

        智谱接口返回按天、按模型、按 tokenType 的明细，
        我们需要的是 (date, model) 粒度，已经在 client 中聚合好了。
        """
        # 拉取这一天的数据
        start_iso = f"{day.isoformat()}T00:00:00+08:00"
        end_iso = f"{day.isoformat()}T23:59:59+08:00"

        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)

        # 从 grouped 数据中提取这一天的所有模型
        grouped = result.get("grouped", {})
        target_date = day.isoformat()

        rows: list[ModelRow] = []

        for (date, model_code), m in grouped.items():
            if date != target_date:
                continue

            # 智谱返回的 token 字段说明：
            # - prompt_tokens: 已经是输入 token（不含 cache）
            # - cache_read_tokens: 缓存命中 token
            # - cache_write_tokens: 缓存写入 token
            #
            # 我们的字段语义：
            # - prompt_tokens: 含 cache 的完整 input = prompt + cache_read + cache_write
            # - completion_tokens: 输出
            # - cache_read_tokens / cache_write_tokens: 单独拆出

            prompt_only = m.get("prompt_tokens") or 0
            cache_read = m.get("cache_read_tokens") or 0
            # None = 上游账单无"缓存写入"行 (限时免费, 未暴露), 不伪造 0
            cache_write = m.get("cache_write_tokens")
            completion = m.get("completion_tokens") or 0

            # 使用 pack_input 合并成完整的 prompt_tokens
            prompt_full = pack_input(prompt_only, cache_read, cache_write)
            total = pack_total(prompt_full, completion)

            rows.append(ModelRow(
                model=normalize_model_name(model_code) or model_code,
                prompt_tokens=prompt_full,
                completion_tokens=completion,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                total_tokens=total,
                request_count=m.get("total_count"),
                image_count=None,
                cost_native=m.get("total_cost"),
            ))

        return rows

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (apiKey, model) 拆分账单行, 写 vendor_apikey_usage_daily.

        智谱账单行自带 apiKey 字段. 聚合语义与 fetch_one_day 完全一致:
        usageCount 按 tokenType 分桶, apiUsage 累加为请求数, 费用取
        originalAmount (原价) — 保证 key 表各 key 求和 == 主表同口径.
        上游接口失败 / 未登录 → 返空 list (key 表不动).
        """
        state_file = _get_state_file(self.vendor["base_url"])
        if not state_file.exists():
            return []
        try:
            _, token, extra_headers = _load_session(state_file)
            rows = fetch_bills_by_day(
                self.vendor["base_url"], token,
                day.strftime("%Y-%m"), extra_headers,
            )
        except Exception:
            return []

        target = day.isoformat()
        grouped: dict[tuple[str, str], dict] = {}
        for row in rows:
            if row.get("billingDate") != target:
                continue
            api_key = (row.get("apiKey") or "").strip()
            model_code = row.get("modelCode")
            if not api_key or not model_code:
                continue  # key 表只存有 key 的行
            g = grouped.setdefault((api_key, model_code), {
                "prompt": 0, "completion": 0,
                "cache_read": 0, "cache_write": None,  # None-until-seen: 无"缓存写入"行时不伪造 0
                "request": 0, "cost": 0.0,
            })
            usage = int(row.get("usageCount") or 0)
            token_type = row.get("tokenType")
            if token_type == "输入":
                g["prompt"] += usage
            elif token_type == "输出":
                g["completion"] += usage
            elif token_type == "缓存命中":
                g["cache_read"] += usage
            elif token_type == "缓存写入":
                g["cache_write"] = (g["cache_write"] or 0) + usage
            g["request"] += int(row.get("apiUsage") or 0)
            g["cost"] += float(row.get("originalAmount") or 0)

        out: list[ApiKeyRow] = []
        for (api_key, model_code), g in grouped.items():
            prompt_full = pack_input(g["prompt"], g["cache_read"], g["cache_write"])
            completion = g["completion"]
            out.append(ApiKeyRow(
                api_key=api_key,
                model=normalize_model_name(model_code) or model_code,
                prompt_tokens=prompt_full,
                completion_tokens=completion,
                cache_read_tokens=g["cache_read"],
                cache_write_tokens=g["cache_write"],
                total_tokens=pack_total(prompt_full, completion),
                request_count=g["request"],
                image_count=None,
                cost_native=round(g["cost"], 6),
            ))
        return out
