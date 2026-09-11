"""上游 LiteLLM 定价 (model_prices_and_context_window.json) 加载器。

数据流:
  1. 启动时调用 load_upstream() → 优先吃本地缓存 (data/upstream_model_prices.json), 24h 过期再去 GitHub 拉。
  2. 每条上游记录转换成本项目内的 PricingConfig dict (with_cache 或 standard)。
  3. 返回 {(model_lower, provider_lower|None): cfg_dict}, 由 pricing_cache_service 当 base 层用。

只覆盖文本 token (input_cost_per_token + output_cost_per_token), 图像/视频/音频/embedding 跳过。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

UPSTREAM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "upstream_model_prices.json"
CACHE_TTL_SEC = 24 * 3600


def _fetch_remote() -> dict | None:
    try:
        r = requests.get(UPSTREAM_URL, timeout=15)
        r.raise_for_status()
        data = r.json()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("Fetched upstream pricing: %d entries -> %s", len(data), CACHE_FILE)
        return data
    except Exception as e:
        logger.warning("Fetch upstream pricing failed: %s", e)
        return None


def _load_raw() -> dict:
    """优先吃 24h 内的本地缓存; 失效 + 拉取失败时仍用旧缓存; 都没有则返回 {}。"""
    fresh = CACHE_FILE.exists() and (time.time() - CACHE_FILE.stat().st_mtime) < CACHE_TTL_SEC
    if fresh:
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = _fetch_remote()
    if data is not None:
        return data
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _per_1m(token_cost: float | None) -> float | None:
    """USD/token → USD/1M tokens。"""
    if token_cost is None:
        return None
    return float(token_cost) * 1_000_000


def _to_pricing_config(entry: dict) -> dict | None:
    """把上游条目转成本项目的 PricingConfig dict。无文本定价的 (image/video/audio) 返回 None 跳过。"""
    inp = entry.get("input_cost_per_token")
    outp = entry.get("output_cost_per_token")
    if inp is None or outp is None:
        return None

    cache_read = entry.get("cache_read_input_token_cost")
    cache_write = entry.get("cache_creation_input_token_cost")
    cache_write_1h = entry.get("cache_creation_input_token_cost_above_1hr")

    # 有 1h 长缓存 → advanced; 仅 5min cache_creation → with_cache; 无缓存 → standard
    if cache_write_1h is not None:
        cfg = {
            "type": "advanced",
            "input_tiers": [{"up_to_k": None, "price_per_1m": _per_1m(inp)}],
            "output_tiers": [{"up_to_k": None, "price_per_1m": _per_1m(outp)}],
            "cache_read_tiers": [{"up_to_k": None, "price_per_1m": _per_1m(cache_read)}] if cache_read is not None else None,
            "cache_write_5min_per_1m": _per_1m(cache_write),
            "cache_write_1h_per_1m": _per_1m(cache_write_1h),
            "currency": "USD",
        }
    elif cache_read is not None or cache_write is not None:
        cfg = {
            "type": "with_cache",
            "input_per_1m": _per_1m(inp),
            "output_per_1m": _per_1m(outp),
            "cache_write_per_1m": _per_1m(cache_write) or 0,
            "cache_read_per_1m": _per_1m(cache_read) or 0,
            "currency": "USD",
        }
    else:
        cfg = {
            "type": "standard",
            "input_per_1m": _per_1m(inp),
            "output_per_1m": _per_1m(outp),
            "currency": "USD",
        }
    return cfg


def load_upstream() -> dict[tuple[str, str | None], dict]:
    """返回 {(model_lower, provider_lower|None): pricing_cfg_dict}。"""
    raw = _load_raw()
    out: dict[tuple[str, str | None], dict] = {}
    for name, entry in raw.items():
        if name == "sample_spec" or not isinstance(entry, dict):
            continue
        cfg = _to_pricing_config(entry)
        if not cfg:
            continue
        provider = entry.get("litellm_provider")
        # 1) 不带 provider, 默认 fallback
        out.setdefault((name.lower(), None), cfg)
        # 2) 带 provider, 供应商专属
        if provider:
            out[(name.lower(), provider.lower())] = cfg
        # 3) 上游名常带 "provider/model" 前缀, 拆出去前缀的裸名也建一条 (不覆盖)
        if "/" in name:
            bare = name.split("/", 1)[1].lower()
            out.setdefault((bare, None), cfg)
            if provider:
                out.setdefault((bare, provider.lower()), cfg)
    logger.info("Loaded %d upstream pricing keys (text-token models only)", len(out))
    return out
