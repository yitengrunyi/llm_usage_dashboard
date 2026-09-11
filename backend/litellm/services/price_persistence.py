"""
定价持久化服务：将 model_prices 表数据导出/导入 JSON 文件。

用途：
  - DB 表可能被外部重建（临时表），每次增删改查后自动导出到 JSON 文件
  - 启动时如果 DB 为空，从 JSON 文件恢复；文件也不存在则用 builtin_prices 兜底
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import SessionLocal
from litellm.models import ModelPrice

logger = logging.getLogger(__name__)

# 持久化文件路径
PRICES_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "model_prices.json"


def _row_to_dict(row: ModelPrice) -> dict:
    return {
        "model_name": row.model_name,
        "model_group": row.model_group,
        "provider": row.provider,
        "model_family": row.model_family,
        "pricing_config": row.pricing_config,
        "currency": row.currency,
        "is_active": row.is_active,
        "aliases": row.aliases,
        "note": row.note,
    }


async def export_to_file(db: AsyncSession | None = None) -> None:
    """将 DB 中所有 model_prices 导出到 JSON 文件。"""
    try:
        if db is None:
            async with SessionLocal() as db:
                await _do_export(db)
        else:
            await _do_export(db)
    except Exception as e:
        logger.warning("Failed to export prices to file: %s", e)


async def _do_export(db: AsyncSession) -> None:
    rows = (await db.execute(select(ModelPrice).order_by(ModelPrice.model_name, ModelPrice.provider))).scalars().all()
    data = [_row_to_dict(r) for r in rows]
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Exported %d prices to %s", len(data), PRICES_FILE)


async def seed_from_file_or_builtin() -> None:
    """DB 为空时，从 JSON 文件恢复；文件不存在则用 builtin_prices 兜底。"""
    try:
        async with SessionLocal() as db:
            count = (await db.execute(select(func.count()).select_from(ModelPrice))).scalar() or 0
            if count > 0:
                logger.info("model_prices has %d rows, skipping seed.", count)
                return

            # 优先从文件加载
            items = _load_from_file()
            if not items:
                items = _load_from_builtin()

            if not items:
                logger.info("No seed data available.")
                return

            logger.info("Seeding %d prices into model_prices...", len(items))
            for item in items:
                row = ModelPrice(
                    model_name=item["model_name"],
                    model_group=item.get("model_group"),
                    provider=item.get("provider"),
                    model_family=item.get("model_family", "generic"),
                    pricing_config=item["pricing_config"],
                    currency=item.get("currency", "USD"),
                    is_active=item.get("is_active", True),
                    aliases=item.get("aliases"),
                    note=item.get("note"),
                )
                db.add(row)
            await db.commit()
            logger.info("Seeded %d prices.", len(items))
    except Exception as e:
        logger.warning("Failed to seed prices: %s", e)


def _load_from_file() -> list[dict] | None:
    if not PRICES_FILE.exists():
        logger.info("No prices file at %s", PRICES_FILE)
        return None
    try:
        data = json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) > 0:
            logger.info("Loaded %d prices from %s", len(data), PRICES_FILE)
            return data
    except Exception as e:
        logger.warning("Failed to read prices file: %s", e)
    return None


def _load_from_builtin() -> list[dict]:
    from litellm.pricing.builtin_prices import BUILTIN_STANDARD_PRICES

    logger.info("Using %d builtin prices as fallback.", len(BUILTIN_STANDARD_PRICES))
    return [
        {
            "model_name": name,
            "provider": None,
            "model_family": "generic",
            "pricing_config": config,
            "currency": config.get("currency", "USD"),
            "is_active": True,
        }
        for name, config in BUILTIN_STANDARD_PRICES.items()
    ]
