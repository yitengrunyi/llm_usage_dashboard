"""Grok (xAI) adapter. 复用 grok_client.fetch_vendor_usage, 按天调一次拿一天的 model 聚合.

时区: xAI usage analytics 支持指定 IANA 时区切天 (GROK_TIMEZONE, 默认 Asia/Shanghai),
跟其他 vendor 的 CST 自然日对齐 → cron 直接走 03:00 主批次, 不需要 openai 那种单独 PT cron.

token 拆分: 上游只文档化了 usd 金额指标; token/请求类指标靠 grok_client 进程内探测,
拿不到时 prompt/completion/cache/request_count 全为 NULL (前端显示 —), cost 永远有值.
"""
from __future__ import annotations

import datetime as dt

from grok_client import _TZ, fetch_total_usd, fetch_usage_by_api_key, fetch_vendor_usage
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total


class GrokAdapter(VendorAdapter):
    vendor_id = "grok"
    source = "xai_management_api"
    native_currency = "USD"
    # cache 指标未文档化 (探测到时 model 行会带值); vendor 层 has_cache_detail 保守 False
    has_cache_detail = False

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        # GROK_TIMEZONE 这一天的 [00:00, 次日 00:00) — 左闭右开 (endTime exclusive)
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=_TZ)
        end_dt = start_dt + dt.timedelta(days=1)

        result = fetch_vendor_usage(self.vendor, start_dt.isoformat(), end_dt.isoformat())
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            cr = m.get("cache_read_tokens")
            cw = m.get("cache_write_tokens")  # xAI cache 写计费未知 → None
            prompt_full = pack_input(m.get("prompt_tokens"), cr, cw)
            comp = m.get("completion_tokens")
            # client 的 total_tokens: in/out 都有 = full_in+out; 缺一边 = 上游
            # total_tokens 指标兜出的值。不能直接 pack_total — 它把缺的一边当 0, 低估。
            total = m.get("total_tokens")
            if total is None:
                total = pack_total(prompt_full, comp)
            rows.append(ModelRow(
                model=model_name,
                prompt_tokens=prompt_full,
                completion_tokens=comp,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                total_tokens=total,
                request_count=m.get("total_count"),
                image_count=m.get("image_count"),  # 上游无图片张数指标 → None
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def fetch_upstream_total_cost(self, day: dt.date) -> float | None:
        """TIME_UNIT_NONE 整窗 usd 总额 — silent-empty sanity check 用。

        返 0 = 上游确认这天真无消耗 (job.py 不报警); 拉不到 → None 走 14 天 avg 启发式。
        """
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=_TZ)
        end_dt = start_dt + dt.timedelta(days=1)
        try:
            return float(fetch_total_usd(
                self.vendor, start_dt.isoformat(), end_dt.isoformat()))
        except Exception:
            return None

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 api_key_id 分组的 usd 明细 — vendor 详情页 "按 API key 筛选" 用。

        groupBy api_key_id 未文档化但实测可用, groupLabels 直接带人读 key 名。
        上游不按 key 出 token 指标 → token 列全 NULL ("—"), cost_native 为 usd。
        """
        by_key = fetch_usage_by_api_key(self.vendor, day, day)
        rows = []
        for (key_label, model), by_date in by_key.items():
            rows.append(ApiKeyRow(
                api_key=key_label,
                model=model,
                prompt_tokens=None,
                completion_tokens=None,
                cache_read_tokens=None,
                cache_write_tokens=None,
                total_tokens=None,
                request_count=None,
                image_count=None,
                cost_native=round(sum(by_date.values()), 6),
            ))
        return rows
