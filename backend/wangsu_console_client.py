"""wangsu 控制台分析接口客户端 — 令牌 (API key) 维度用量.

背景: open API (wangsu_client.py) 只有模型聚合, 没有 key 维度. 控制台
/v2/eca/ai-gateway/analysis/new/detail 支持 tokenName × modelName 交叉维度,
且返回**真实金额** (按模型分币种, "$..." / "¥..."). 鉴权是控制台 Cookie 会话
(SESSION + ONE_SITE_SESSION + WS_sid), 存 .env WANGSU_CONSOLE_COOKIE,
会话失效需人工从浏览器重新复制.

上游校验: tokenIdList 和 aiGatewayNameList **都必填** (空任一 → 10000 参数
有误), 控制台自己也是全量带上的. 清单人工维护在 wangsu_tokens.json —
tokenIds 支持 "ALL" 哨兵 (全选语义, 新令牌自动覆盖); gateways 不支持 ALL,
新增网关要手动补.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from datetime import timedelta, timezone
from pathlib import Path

import requests

from utils import normalize_model_name

CST = timezone(timedelta(hours=8))
URL = "https://edge-ai.console.wangsu.com/v2/eca/ai-gateway/analysis/new/detail"
FILTERS_FILE = Path(__file__).parent / "wangsu_tokens.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

log = logging.getLogger("wangsu_console")

_METRICS = ["amount", "visit", "total_tokens", "prompt_tokens",
            "completion_tokens", "prompt_cache_tokens", "prompt_creation_tokens"]


def load_filters() -> tuple[list[str], list[str]]:
    cfg = json.loads(FILTERS_FILE.read_text(encoding="utf-8"))
    return cfg.get("tokenIds") or [], cfg.get("gateways") or []


def _parse_amount(s: str | None) -> tuple[float, str]:
    """'$48434.14'→(48434.14,'USD'); '¥0.99'→(0.99,'CNY');
    '<$0.01'→(0.0,'USD'); '$0.00'→(0.0,'USD')."""
    if not s:
        return 0.0, "USD"
    s = s.strip()
    cur = "CNY" if "¥" in s else "USD"
    if s.startswith("<"):
        return 0.0, cur  # <$0.01 记 0, 量级可忽略
    m = re.search(r"[\d.]+", s)
    try:
        return (float(m.group()) if m else 0.0), cur
    except ValueError:
        return 0.0, cur


def _map_model(name: str | None) -> str:
    """控制台模型名 → 主表归一化名.

    'Claude Opus 4.6' → 'claude-opus-4-6' (claude 系主表用横线);
    'gpt-5.4' / 'gemini-3.1-pro-preview' 保持点号 (主表就是 'gpt-5.4' / 'gemini-3.1-pro').
    点转横线只对 claude 系做 — 全做会把 gpt-5.4 错变 gpt-5-4 (主表无此名).
    """
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    if s.startswith("claude"):
        s = re.sub(r"(\d+)\.(\d+)", r"\1-\2", s)
    return normalize_model_name(s) or s or "unknown"


def _split_token(token_name: str) -> tuple[str, str | None]:
    """'gw-2 (ID:gw-0002@unit1)' → ('gw-2', 'gw-0002@unit1')."""
    m = re.match(r"^(.*?)\s*\(ID:([^)]+)\)\s*$", (token_name or "").strip())
    if m:
        return (m.group(1).strip() or m.group(2).strip()), m.group(2).strip()
    return (token_name or "").strip() or "-", None


def fetch_token_day_rows(vendor: dict, day: dt.date) -> list[dict]:
    """单天 tokenName×modelName 用量. 返回原始行 (金额保留原币种).

    每行: {token_name, token_id, model, amount, currency, visit,
           prompt_tokens, completion_tokens, cache_read, cache_write}
    口径: prompt_tokens 是完整输入 (含 cache_read + cache_write), 与主表
    vendor_model_usage_daily.prompt_tokens 同口径, 调用方直接用, 不要再 pack_input.
    同一 (token, model) 可能因币种拆成多行 (e.g. MiniMax ¥ + 其他 $), 调用方合并.
    Cookie 缺失/失效抛异常, 由调用方决定降级策略.
    """
    cookie = vendor.get("console_cookie") or ""
    if not cookie:
        raise RuntimeError("WANGSU_CONSOLE_COOKIE 未配置 (.env), 无法拉令牌维度")

    token_ids, gateways = load_filters()
    if not token_ids or not gateways:
        raise RuntimeError("wangsu_tokens.json 缺 tokenIds/gateways, 上游要求两者都非空")

    start_ms = int(dt.datetime(day.year, day.month, day.day, tzinfo=CST).timestamp() * 1000)
    end_ms = start_ms + 86_399_000
    ts = str(int(time.time() * 1000))
    body = {
        "startDate": start_ms, "endDate": end_ms,
        "dimensionList": ["tokenName", "modelName"],
        "metricList": _METRICS,
        "tokenIdList": token_ids,
        "groupIdList": [], "modelList": [], "modelApplicationList": [],
        "aiGatewayNameList": gateways,
        "currencyEnum": "USD",
        "orderBy": [{"field": "amount", "order": "desc"}],
        "instanceIdList": [],
    }
    r = requests.post(
        f"{URL}?t={ts}&lang=zh-cn",
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8",
            "cookie": cookie,
            "menucode": "ai_gateway_analysis",
            "origin": "https://edge-ai.console.wangsu.com",
            "productcode": "EdgeAIGateway",
            "referer": "https://edge-ai.console.wangsu.com/v2/index",
            "timestamp": ts, "timezone": "GMT+8:00",
            "user-agent": UA,
        },
        json=body, timeout=60,
    )
    d = r.json()
    if d.get("code") != "0":
        raise RuntimeError(f"wangsu 控制台接口错误 code={d.get('code')} msg={d.get('message')}")

    out: list[dict] = []
    for row in d.get("data", {}).get("rows") or []:
        token_name, token_id = _split_token(row.get("tokenName"))
        amount, currency = _parse_amount(row.get("amount"))
        out.append({
            "token_name": token_name,
            "token_id": token_id,
            "model": _map_model(row.get("modelName")),
            "amount": amount,
            "currency": currency,
            "visit": int(row.get("visit") or 0),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
            "completion_tokens": int(row.get("completion_tokens") or 0),
            "cache_read": int(row.get("prompt_cache_tokens") or 0),
            "cache_write": int(row.get("prompt_creation_tokens") or 0),
        })
    return out
