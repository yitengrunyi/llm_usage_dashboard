"""
实时汇率服务：从免费公开 API 获取 USD/CNY 汇率，内存缓存 1 小时。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_FALLBACK_USD_TO_CNY = 7.2
_CACHE_TTL_SECONDS = 3600  # 1 小时

_cached_rate: float | None = None
_cached_at: float = 0.0


async def get_usd_to_cny() -> float:
    """获取 1 USD = ? CNY 汇率，带 1 小时内存缓存，失败时 fallback 7.2。"""
    global _cached_rate, _cached_at

    now = time.monotonic()
    if _cached_rate is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_rate

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/USD")
            resp.raise_for_status()
            data = resp.json()
            rate = float(data["rates"]["CNY"])
            _cached_rate = rate
            _cached_at = now
            logger.info("汇率更新成功: 1 USD = %.4f CNY", rate)
            return rate
    except Exception:
        logger.warning("汇率 API 请求失败，使用 fallback: 1 USD = %.1f CNY", _FALLBACK_USD_TO_CNY)
        if _cached_rate is not None:
            return _cached_rate
        return _FALLBACK_USD_TO_CNY


def convert_cost(cost: float, currency: str, usd_to_cny: float) -> tuple[float, float]:
    """
    根据原始币种和汇率，返回 (cost_usd, cost_cny)。
    currency 为 "元" 或 "CNY" 时视为人民币，"USD" 视为美元。
    """
    if currency in ("元", "CNY"):
        return (round(cost / usd_to_cny, 8), round(cost, 8))
    else:
        return (round(cost, 8), round(cost * usd_to_cny, 8))
