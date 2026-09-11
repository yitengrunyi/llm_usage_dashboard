"""Grok (xAI) 平台客户端 — Management API usage analytics.

数据源: POST https://management-api.x.ai/v1/billing/teams/{team_id}/usage
- groupBy=["description"] 按计费项(≈模型)拆分, e.g. "Chat grok-4-0709" / "grok-2-image-1212"
  (官方 docs.x.ai rest-api-reference/management/billing, 2026-08 验证)
- timeUnit=TIME_UNIT_DAY; timeRange.timezone 决定按哪个时区切天
  (GROK_TIMEZONE, 默认 Asia/Shanghai → 跟其他 vendor 的 CST 自然日对齐, cron 走 03:00 主批次)
- values 里官方只文档化了 "usd" (AGGREGATION_SUM, 美元金额)。
  token/请求类指标名未公开 (console.x.ai Usage Explorer 有 Tokens 维度, 数据存在) —
  进程内首次调用会整批探测候选指标名, 400 就退回仅 usd, token 列显示 "—" (NULL 语义)。
  指标名以后文档化了往 _EXTRA_VALUE_CANDIDATES 里补即可。

凭据: Management Key (≠ 推理用的 API key!) — console.x.ai → Settings → Management Keys
创建 (Management Keys Read+Write 权限), 填 backend/.env 的 GROK_MANAGEMENT_KEY。
team id 默认 "default", 特殊情况 GROK_TEAM_ID 覆盖。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback (我们 3.12)
    ZoneInfo = None

log = logging.getLogger("grok.client")

# 国内服务器到 management-api.x.ai 可能被墙, 走 GROK_PROXY 透明代理 (同 OPENAI_PROXY 模式)
BASE_URL = (os.environ.get("GROK_MGMT_BASE_URL") or "https://management-api.x.ai").rstrip("/")
_TIMEOUT = float(os.environ.get("GROK_TIMEOUT") or 30)
_PROXY = os.environ.get("GROK_PROXY") or None
_PROXIES = {"http": _PROXIES, "https": _PROXIES} if _PROXY else None

_TZ_NAME = os.environ.get("GROK_TIMEZONE") or "Asia/Shanghai"
_TZ = ZoneInfo(_TZ_NAME) if ZoneInfo else datetime.now().astimezone().tzinfo

_USD_VALUE = {"name": "usd", "aggregation": "AGGREGATION_SUM"}

# token/请求类指标候选 (官方未文档化, 名字是猜的 — 探测 400 就整体放弃, 不影响 usd)
_EXTRA_VALUE_CANDIDATES = [
    {"name": "input_tokens", "aggregation": "AGGREGATION_SUM"},
    {"name": "output_tokens", "aggregation": "AGGREGATION_SUM"},
    {"name": "cached_tokens", "aggregation": "AGGREGATION_SUM"},
    {"name": "total_tokens", "aggregation": "AGGREGATION_SUM"},
    {"name": "requests", "aggregation": "AGGREGATION_SUM"},
]
_CANDIDATE_NAMES = ["usd"] + [v["name"] for v in _EXTRA_VALUE_CANDIDATES]

# 进程内探测结果 (None = 未探测)。并发首调可能双探测, 无害 (幂等请求)。
_active_value_names: list[str] | None = None


def _values_for(names: list[str]) -> list[dict]:
    return [{"name": n, "aggregation": "AGGREGATION_SUM"} for n in names]


class _AnalyticsError(RuntimeError):
    """带 HTTP status 的 analytics 调用错误 (探测分支要按 status 分流, 不能靠子串匹配)."""
    def __init__(self, msg: str, status_code: int | None = None):
        super().__init__(msg)
        self.status_code = status_code


def _request(management_key: str, team_id: str, body: dict) -> dict:
    """POST usage analytics。5xx/429 退避重试 3 次 (1s/2s/4s); 4xx 立刻抛 (重试无用)。"""
    url = f"{BASE_URL}/v1/billing/teams/{team_id}/usage"
    last_err = None
    for attempt in range(3):
        r = requests.post(
            url, json=body,
            headers={"Authorization": f"Bearer {management_key}"},
            timeout=_TIMEOUT, proxies=_PROXIES,
        )
        if r.status_code == 200:
            return r.json()
        detail = f"Grok usage API status={r.status_code}: {r.text[:200]}"
        if r.status_code in (401, 403):
            raise PermissionError(
                detail + " — GROK_MANAGEMENT_KEY 无效或权限不足 "
                "(console.x.ai → Settings → Management Keys 创建, ≠ 推理 API key)"
            )
        if r.status_code >= 500 or r.status_code == 429:
            last_err = detail
            time.sleep(2 ** attempt)
            continue
        raise _AnalyticsError(detail, r.status_code)
    raise _AnalyticsError(f"Grok usage API 重试 3 次仍失败: {last_err}")


def _fetch_time_series(management_key: str, team_id: str, start_dt: datetime,
                       end_dt: datetime, time_unit: str, group_by: list,
                       values: list[dict]) -> dict:
    """一次 analytics 请求。时间窗转成 GROK_TIMEZONE 的 wall-clock 串 (API 要求)。"""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=_TZ)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=_TZ)
    body = {"analyticsRequest": {
        "timeRange": {
            "startTime": start_dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end_dt.astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": _TZ_NAME,
        },
        "timeUnit": time_unit,
        "values": values,
        "groupBy": group_by,
        "filters": [],
    }}
    resp = _request(management_key, team_id, body)
    if resp.get("limitReached"):
        # 基数上限, 结果被截断 — 宁可失败也不要静默少数据
        raise RuntimeError("Grok usage API limitReached=true (结果被截断), 缩小时间窗/分组维度")
    return resp


def _clean_model_name(label: str) -> str:
    """计费项描述 → 模型名。只剥类别前缀; 日期后缀是 xAI 官方命名 (grok-2-1212) 保留。"""
    for prefix in ("Chat ", "Image ", "Embedding ", "Function ", "API "):
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def _point_date(ts: str) -> str:
    """dataPoint.timestamp 是 UTC ISO 串; 转回 GROK_TIMEZONE 取日期 (桶=该时区的天)。"""
    t = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    return t.astimezone(_TZ).strftime("%Y-%m-%d")


def _credentials(vendor: dict) -> tuple[str, str]:
    management_key = vendor.get("management_key")
    if not management_key:
        raise PermissionError(
            "Grok vendor 缺 GROK_MANAGEMENT_KEY — 在 console.x.ai → Settings → "
            "Management Keys 创建 (≠ 推理 API key), 填到 backend/.env"
        )
    return management_key, vendor.get("team_id") or "default"


def fetch_vendor_usage(vendor: dict, start_time: str, end_time: str) -> dict:
    """统一格式 (USD)。per-model / per-day 金额来自 usage analytics (官方权威)。"""
    global _active_value_names
    management_key, team_id = _credentials(vendor)
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)

    # 首次调用探测 token 指标; 之后进程内直接用探测结果
    if _active_value_names is None:
        try:
            resp = _fetch_time_series(management_key, team_id, start_dt, end_dt,
                                      "TIME_UNIT_DAY", ["description"], _values_for(_CANDIDATE_NAMES))
            _active_value_names = list(_CANDIDATE_NAMES)
            log.info(f"[grok] usage 指标探测成功: {_CANDIDATE_NAMES}")
        except _AnalyticsError as e:
            if e.status_code != 400:
                raise
            log.warning(f"[grok] token 指标候选被拒 (400), 退回仅 usd, token 列将显示 —: {e}")
            resp = _fetch_time_series(management_key, team_id, start_dt, end_dt,
                                      "TIME_UNIT_DAY", ["description"], [_USD_VALUE])
            # usd-only 请求成功后才缓存 — 400 若与指标名无关 (如时间窗非法),
            # 回退请求也会失败上抛, 全局不能被锁死成永久禁用 token 指标
            _active_value_names = ["usd"]
    else:
        resp = _fetch_time_series(management_key, team_id, start_dt, end_dt,
                                  "TIME_UNIT_DAY", ["description"],
                                  _values_for(_active_value_names))
    names = _active_value_names
    idx = {n: i for i, n in enumerate(names)}

    # 客户端双侧截断 (openai_client 同款习惯): dataPoints 是 dense 的,
    # 万一上游把窗外桶也吐回来, 这里按 [start, end) 重叠语义滤掉
    win_start = start_dt.astimezone(_TZ)
    win_end = end_dt.astimezone(_TZ)

    # model → date → {metric: 数值}。values[i] 与请求的 values 顺序一一对应;
    # 某指标全窗只回 null → 当"上游不给"丢弃 (防把 unknown 当真 0)。
    per_model: dict[str, dict[str, dict]] = {}
    metric_has_value: set[str] = set()
    for series in resp.get("timeSeries") or []:
        labels = series.get("groupLabels") or series.get("group") or []
        name = _clean_model_name(labels[0] if labels else "unknown")
        for pt in series.get("dataPoints") or []:
            vals = pt.get("values") or []
            date = _point_date(pt.get("timestamp") or "")
            d_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=_TZ)
            if not (d_start < win_end and d_start + timedelta(days=1) > win_start):
                continue
            e = per_model.setdefault(name, {}).setdefault(date, {})
            for metric, i in idx.items():
                raw = vals[i] if i < len(vals) else None
                if raw is None:
                    continue
                metric_has_value.add(metric)
                if metric == "usd":
                    e["usd"] = e.get("usd", 0.0) + float(raw or 0)
                else:
                    e[metric] = e.get(metric, 0) + int(raw or 0)

    models: dict[str, dict] = {}
    daily_cost: dict[str, float] = {}
    for name, by_date in per_model.items():
        agg: dict = {"usd": 0.0}
        for date, m in by_date.items():
            daily_cost[date] = daily_cost.get(date, 0.0) + m.get("usd", 0.0)
            agg["usd"] += m.get("usd", 0.0)
            for metric in names:
                if metric != "usd" and metric in m:
                    agg[metric] = agg.get(metric, 0) + m[metric]

        full_in = agg.get("input_tokens")
        out = agg.get("output_tokens")
        cache_r = agg.get("cached_tokens")
        # xAI per-request 口径 prompt_tokens 含 cached → 这里 input_tokens 当完整 input,
        # 对前端"非缓存 prompt"口径现拆; cache 拿不到就视 input 全为非缓存。
        if full_in is not None and cache_r is not None:
            non_cache_in = max(0, full_in - cache_r)
        else:
            non_cache_in = full_in
        if full_in is not None and out is not None:
            total = full_in + out
        else:
            total = agg.get("total_tokens")

        models[name] = {
            "prompt_tokens": non_cache_in,
            "completion_tokens": out,
            "cache_tokens": cache_r,                 # 兼容老字段 (= cache_read)
            "cache_read_tokens": cache_r,
            "cache_write_tokens": None,              # xAI 有无 cache 写计费未知 → NULL
            "total_tokens": total,
            "total_count": agg.get("requests"),
            "total_cost": round(agg["usd"], 6),
        }

    daily = sorted(
        ({"date": d, "cost": round(c, 6)} for d, c in daily_cost.items()),
        key=lambda x: x["date"],
    )
    return {
        "total_cost": round(sum(daily_cost.values()), 6),
        "models": models,
        "daily": daily,
        "vendor_id": vendor["id"],
        "vendor_name": vendor["name"],
        "currency": vendor.get("currency", "USD"),
    }


def fetch_total_usd(vendor: dict, start_time: str, end_time: str) -> float:
    """整窗 usd 总额 — TIME_UNIT_NONE 单桶不分模型, silent-empty sanity check 用。
    拉不到会抛, 由 adapter 兜底转 None。只用文档化的 usd 指标, 不掺探测。"""
    management_key, team_id = _credentials(vendor)
    resp = _fetch_time_series(management_key, team_id,
                              datetime.fromisoformat(start_time),
                              datetime.fromisoformat(end_time),
                              "TIME_UNIT_NONE", [], [_USD_VALUE])
    total = 0.0
    for series in resp.get("timeSeries") or []:
        for pt in series.get("dataPoints") or []:
            vals = pt.get("values") or []
            total += float(vals[0] or 0)   # values 只有 [usd]
    return total


# ---------------------------------------------------------------------------
# token 精确数据 — 发票明细 (usage analytics 只有 usd 指标, token 数拿不到;
# 但 invoices / invoice preview 的 lines[] 带 per-model per-unitType 的 numUnits)
# 账期 = UTC 自然月 (2026-08 实测: preview 总额 == analytics UTC 月累计)。
# ---------------------------------------------------------------------------

def _get(path: str, management_key: str) -> dict:
    """GET management API。5xx/429 退避重试同 _request; 4xx 立刻抛。"""
    url = f"{BASE_URL}{path}"
    last_err = None
    for attempt in range(3):
        r = requests.get(url, headers={"Authorization": f"Bearer {management_key}"},
                         timeout=_TIMEOUT, proxies=_PROXIES)
        if r.status_code == 200:
            return r.json()
        detail = f"Grok management API GET {path} status={r.status_code}: {r.text[:200]}"
        if r.status_code in (401, 403):
            raise PermissionError(detail + " — GROK_MANAGEMENT_KEY 无效或权限不足")
        if r.status_code >= 500 or r.status_code == 429:
            last_err = detail
            time.sleep(2 ** attempt)
            continue
        raise _AnalyticsError(detail, r.status_code)
    raise _AnalyticsError(f"Grok management API GET {path} 重试 3 次仍失败: {last_err}")


def fetch_cycle_lines(vendor: dict, year: int, month: int) -> dict[tuple[str, str], dict]:
    """某账期 (UTC 自然月) 的 {(model, unitType): {tokens, usd}} 精确总量。

    已结账期 → GET /v1/billing/teams/{id}/invoices (monthly.billingCycle 匹配, 最终权威);
    当前账期 → GET .../postpaid/invoice/preview (billingCycle 匹配, 随用随出)。
    充值凭证 (unitType=prepaid_tokens) 不是用量, 滤掉。无该账期数据时抛错 (宁失败不编造)。
    """
    management_key, team_id = _credentials(vendor)
    lines_batches: list[list[dict]] = []
    for inv in (_get(f"/v1/billing/teams/{team_id}/invoices", management_key)
                .get("invoices") or []):
        cyc = (inv.get("monthly") or {}).get("billingCycle") or {}
        if cyc.get("year") == year and cyc.get("month") == month:
            lines_batches.append(inv.get("lines") or [])
    if not lines_batches:
        prev = _get(f"/v1/billing/teams/{team_id}/postpaid/invoice/preview", management_key)
        cyc = prev.get("billingCycle") or {}
        if cyc.get("year") == year and cyc.get("month") == month:
            lines_batches.append((prev.get("coreInvoice") or {}).get("lines") or [])
    if not lines_batches:
        raise RuntimeError(f"Grok 账期 {year}-{month:02d} 无发票/preview 数据 (该月无消耗或接口异常)")

    agg: dict[tuple[str, str], dict] = {}
    for lines in lines_batches:
        for l in lines:
            if l.get("unitType") == "prepaid_tokens":
                continue
            key = (_clean_model_name(l.get("description") or ""), l.get("unitType") or "")
            e = agg.setdefault(key, {"tokens": 0, "usd": 0.0})
            e["tokens"] += int(l.get("numUnits") or 0)
            e["usd"] += int(l.get("amount") or 0) / 100.0   # amount 单位 = USD cents
    return agg


def fetch_daily_usd_by_type(vendor: dict, start_day, end_day) -> dict[tuple[str, str], dict[str, float]]:
    """CST 自然日粒度的 {(model, unitType): {date_iso: usd}}。

    groupBy unit_type 未文档化但实测可用 (与发票 lines 的 unitType 同枚举)。
    用途: 把账期精确 token 总量按每日成本形状分摊到天 (见 ingest.grok_token_backfill)。
    """
    series = _fetch_daily_two_dim(vendor, start_day, end_day, ["description", "unit_type"])
    return {(labels[0], labels[1]): by_date for labels, by_date in series.items()}


def fetch_usage_by_api_key(vendor: dict, start_day, end_day) -> dict[tuple[str, str], dict[str, float]]:
    """CST 自然日粒度的 {(api_key 标签, model): {date_iso: usd}} — 供 vendor_apikey_usage_daily。

    groupBy api_key_id 未文档化但实测可用; groupLabels[1] 是人读 key 名
    (e.g. "Default API Key (xai-...WZd0)"), 不用再查 key 列表接口。
    只有 usd (token 拿不到) → key 表 token 列 NULL ("—")。
    """
    series = _fetch_daily_two_dim(vendor, start_day, end_day, ["description", "api_key_id"])
    return {(labels[1], labels[0]): by_date for labels, by_date in series.items()}


def _fetch_daily_two_dim(vendor: dict, start_day, end_day, group_by: list) -> dict[tuple[str, str], dict[str, float]]:
    """usage analytics 一次请求, 返回 {(label1, label2): {date_iso: usd}}。CST 日桶, 窗外滤掉。"""
    management_key, team_id = _credentials(vendor)
    start_dt = datetime(start_day.year, start_day.month, start_day.day, tzinfo=_TZ)
    end_dt = start_dt + timedelta(days=(end_day - start_day).days + 1)
    resp = _fetch_time_series(management_key, team_id, start_dt, end_dt,
                              "TIME_UNIT_DAY", group_by, [_USD_VALUE])
    out: dict[tuple[str, str], dict[str, float]] = {}
    for series in resp.get("timeSeries") or []:
        labels = series.get("groupLabels") or series.get("group") or []
        key = (_clean_model_name(labels[0] if labels else "unknown"),
               labels[1] if len(labels) > 1 else "")
        for pt in series.get("dataPoints") or []:
            vals = pt.get("values") or []
            raw = vals[0] if vals else None
            if raw is None:
                continue
            date = _point_date(pt.get("timestamp") or "")
            d_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=_TZ)
            if not (d_start < end_dt and d_start + timedelta(days=1) > start_dt):
                continue
            bucket = out.setdefault(key, {})
            bucket[date] = bucket.get(date, 0.0) + float(raw)
    return out
