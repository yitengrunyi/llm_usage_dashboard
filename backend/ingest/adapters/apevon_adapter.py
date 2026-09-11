"""apevon adapter — codingflow.ai (原 apevon.ai, new-api 系, Playwright session 登录).

走 has_stat_fallback 模式 — 拆快/慢双路径:
- 快路径 fetch_one_day      → /api/statistics/  拿 model 拆分 (上游偶尔漏聚合, 返空)
- 总额接口 fetch_upstream_total_cost → /api/log/self/stat 实时总额 (always works)

statistics 空时, job.py 用 stat 写 vendor_usage_daily.cost (不算 failed), state 不推进.
慢路径补 model 拆分通过 ingest.apevon_slow.slow_fill_day(day) 触发.

fetch_apikey_rows (额外): /api/statistics/ 的行带 token_name (API key 名),
直接按 (token_name, model) 拆成 ApiKeyRow 写 vendor_apikey_usage_daily,
供 vendor 详情页"按 API key 筛选". 跟 fetch_one_day 共用同一批上游行, 不额外请求.
"""
from __future__ import annotations

import datetime as dt

from apevon_client import fetch_vendor_usage, _stat_total_quota, _statistics_rows
from newapi_client import _get_state_file, _load_session
from ingest.adapters.base import ApiKeyRow, ModelRow, VendorAdapter, pack_input, pack_total
from utils import normalize_model_name


class ApevonAdapter(VendorAdapter):
    vendor_id = "apevon"
    source = "apevon_logs"
    native_currency = "USD"
    has_cache_detail = True  # 新 API 系字段全
    has_stat_fallback = True  # statistics 漏聚合时, stat 写 cost 兜底

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
                image_count=None,
                cost_native=float(m.get("total_cost") or 0),
            ))
        return rows

    def _fetch_raw_rows(self, day: dt.date) -> list[dict]:
        """拉这一天原始 statistics 行 (带 token_name). 失败返空 list.

        fetch_one_day 走 fetch_vendor_usage 内部已拉过一次 statistics (按 model 聚合),
        但那条路径不保留原始行 (避免 live JSON 序列化 70 字段). 这里为 key 拆分再拉一次 —
        ingest 是后台日任务, statistics 接口快 (~91 行亚秒级), 多一次调用可接受.
        """
        base_url = self.vendor["base_url"]
        sf = _get_state_file(base_url)
        if not sf.exists():
            return []
        try:
            cookies, user_id, _ = _load_session(sf)
            user_header = self.vendor.get("user_header") or "New-Api-User"
            headers = {user_header: user_id}
            return _statistics_rows(base_url, cookies, headers, day.isoformat(), day.isoformat())
        except Exception:
            return []

    def fetch_apikey_rows(self, day: dt.date) -> list[ApiKeyRow]:
        """按 (token_name, model) 拆分, 写 vendor_apikey_usage_daily.

        拉一次原始 statistics 行抽 token_name. 上游偶尔返空 (跟 fetch_one_day 同样 silent-empty),
        返空 list 即可.
        """
        raw_rows = self._fetch_raw_rows(day)
        quota_per_dollar = self.vendor.get("quota_per_dollar", 1_000_000)
        grouped: dict[tuple[str, str], dict] = {}
        for r in raw_rows:
            token = r.get("token_name") or ""
            if not token:
                continue  # 没 token_name 的行不进 key 表
            model = normalize_model_name(r.get("model_name") or "unknown")
            gkey = (token, model)
            if gkey not in grouped:
                grouped[gkey] = {
                    "prompt": 0, "completion": 0, "cache_r": 0, "cache_w": 0,
                    "total": 0, "req": 0, "cost": 0.0,
                }
            g = grouped[gkey]
            g["prompt"] += int(r.get("prompt_token_used") or r.get("prompt_tokens") or 0)
            g["completion"] += int(r.get("complete_token_used") or r.get("completion_tokens") or 0)
            cr = int(r.get("cache_read_input_token_used") or r.get("cache_read_input_tokens") or 0)
            cw = int(r.get("cache_creation_input_token_used") or r.get("cache_creation_input_tokens") or 0)
            g["cache_r"] += cr
            g["cache_w"] += cw
            g["total"] += int(r.get("token_used") or r.get("total_tokens") or 0)
            g["req"] += int(r.get("request_count") or r.get("count") or 0)
            g["cost"] += int(r.get("quota") or 0) / quota_per_dollar

        out: list[ApiKeyRow] = []
        for (token, model), g in grouped.items():
            prompt_full = pack_input(g["prompt"], g["cache_r"], g["cache_w"])
            out.append(ApiKeyRow(
                api_key=token,
                model=model,
                prompt_tokens=prompt_full,
                completion_tokens=g["completion"],
                cache_read_tokens=g["cache_r"],
                cache_write_tokens=g["cache_w"],
                total_tokens=pack_total(prompt_full, g["completion"]),
                request_count=g["req"],
                image_count=None,
                cost_native=round(g["cost"], 6),
            ))
        return out

    def fetch_upstream_total_cost(self, day: dt.date) -> float | None:
        """apevon 用 /api/log/self/stat — 实时, 不依赖 statistics 隔夜聚合.
        返 USD; 拉不到 (session 失效 / 网络) → return None 让 job.py 走启发式.
        """
        base_url = self.vendor["base_url"]
        sf = _get_state_file(base_url)
        if not sf.exists():
            return None
        try:
            cookies, user_id, _ = _load_session(sf)
            user_header = self.vendor.get("user_header") or "New-Api-User"
            headers = {user_header: user_id}
            start_ts = int(dt.datetime.combine(day, dt.time.min,
                tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
            end_ts = int(dt.datetime.combine(day, dt.time.max,
                tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
            quota = _stat_total_quota(base_url, cookies, headers, start_ts, end_ts)
            quota_per_dollar = self.vendor.get("quota_per_dollar", 1_000_000)
            return round(quota / quota_per_dollar, 6)
        except Exception:
            return None
