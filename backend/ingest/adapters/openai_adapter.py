"""OpenAI adapter. 复用 openai_client.fetch_vendor_usage, 按天调一次拿一天的 model 聚合.

时区: OpenAI 官方账单按 **US/Pacific 自然日** 切, 我们也按 PT 一天一桌入库.
day=2026-05-26 → 抓 PT 2026-05-26 00:00 → 2026-05-27 00:00 (UTC: 5/26 07:00→5/27 07:00 PDT).
跟其他 vendor (CST 自然日) 不同, 前端要提示用户.

OpenAI Admin API 一天 ~10 秒, 90 天 backfill ~15 分钟, cron 增量一天 ~1 分钟.
"""
from __future__ import annotations

import datetime as dt
import logging

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    PT = dt.timezone(dt.timedelta(hours=-8))

from openai_client import _fetch_costs, fetch_usage_by_api_key, fetch_vendor_usage
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total

log = logging.getLogger("ingest.adapters.openai")


class OpenAIAdapter(VendorAdapter):
    vendor_id = "openai"
    source = "openai_admin"
    native_currency = "USD"
    has_cache_detail = True   # 上游有 cache_read, 没 cache_write (置 0)

    def fetch_one_day(self, day: dt.date) -> list[ModelRow]:
        # PT 这一天的 [00:00, 次日 00:00) — 左闭右开, 跟 _fetch_costs 一致
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=PT)
        end_dt = start_dt + dt.timedelta(days=1)
        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()

        result = fetch_vendor_usage(self.vendor, start_iso, end_iso)
        rows: list[ModelRow] = []
        for model_name, m in (result.get("models") or {}).items():
            cr = m.get("cache_read_tokens")
            # usage 接口不报 cache-write token 数 (但 /costs 实际在收 gpt-5.6 家族的写入费,
            # 这些 token 藏在 prompt 里) → None = 未暴露, 不伪造 0
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
                image_count=m.get("image_count", 0),
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def fetch_upstream_total_cost(self, day: dt.date) -> float | None:
        """/costs 单日总额 (USD) — silent-empty sanity check 用。

        返 0 = 上游确认这天真无消耗 (e.g. 流量切走), job.py 静默 success,
        不再走"近 14 天 avg"启发式误报红 banner (2026-07 流量切到 codingflow
        后连报数天就是这个原因)。拉不到 → None 让 job.py 走启发式兜底。
        账单补记漂移由 cron 的 lookback 重拉窗口兜底, 这里只管"有没有"。
        """
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=PT)
        end_dt = start_dt + dt.timedelta(days=1)
        try:
            daily = _fetch_costs(self.vendor["api_key"],
                                 int(start_dt.timestamp()), int(end_dt.timestamp()))
            return float(sum(daily.values()))
        except Exception:
            return None

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 API key (project key) 拆分的用量+成本明细 — 详情页"按 API key 筛选"用.

        client 用 group_by=api_key_id 一次调用拿全 (见 openai_client 探测注释);
        cost = key 级 /costs 权威值按 model 定价权重分摊。per-key 行的
        input_tokens 已是完整输入 (含 cache_read+write), 不再 pack_input
        (会重复加 cache)。"未归属/已删除 key" 合成行吸收 admin key 等残差。

        异常隔离: key 表是平行表 (base.py 契约"主表行为完全不变"), 这里任何
        失败降级为空 list + WARNING — key 表该日保持旧值, 主表不受影响。
        注意自愈边界: PT cron 每天 14 天 lookback DELETE+INSERT, 只覆盖近
        14 天; 更早历史日的回填若降级, 该日 key 行永久缺失 (backfill 不报错),
        回填后应跑一次 Σ key == 主表 的天级对账排查。
        """
        start_dt = dt.datetime.combine(day, dt.time.min, tzinfo=PT)
        end_dt = start_dt + dt.timedelta(days=1)
        try:
            rows = fetch_usage_by_api_key(self.vendor, start_dt.isoformat(), end_dt.isoformat())
        except Exception:
            log.warning(f"[openai] {day} api key 维度拉取失败, key 表该日不动 (主表不受影响)",
                        exc_info=True)
            return []
        return [
            ApiKeyRow(
                api_key=r["api_key"],
                model=r["model"],
                prompt_tokens=r["input_tokens"],
                completion_tokens=r["completion_tokens"],
                cache_read_tokens=r["cache_read_tokens"],
                cache_write_tokens=r["cache_write_tokens"],
                total_tokens=pack_total(r["input_tokens"], r["completion_tokens"]),
                request_count=r["request_count"],
                image_count=None,
                cost_native=r["cost_usd"],
            )
            for r in rows
        ]
