from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.models import ModelPrice
from litellm.schemas import ModelPriceIn


async def list_prices(db: AsyncSession, *, active_only: bool = False) -> list[ModelPrice]:
    stmt = select(ModelPrice).order_by(ModelPrice.model_name)
    if active_only:
        stmt = stmt.where(ModelPrice.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def get_by_model_name(db: AsyncSession, *, model_name: str, provider: str | None = None) -> ModelPrice | None:
    if provider is None:
        stmt = select(ModelPrice).where(
            ModelPrice.model_name == model_name,
            ModelPrice.provider.is_(None),
        )
    else:
        stmt = select(ModelPrice).where(
            ModelPrice.model_name == model_name,
            ModelPrice.provider == provider,
        )
    return (await db.execute(stmt)).scalar_one_or_none()


def _price_key(model_name: str, provider: str | None) -> tuple[str, str | None]:
    return (model_name, provider)


async def get_price_map(db: AsyncSession) -> dict[tuple[str, str | None], ModelPrice]:
    """返回 (model_name, provider) -> ModelPrice。provider 为空表示该模型默认单价。"""
    rows = await list_prices(db, active_only=True)
    return {_price_key(r.model_name, r.provider): r for r in rows}


async def create_price(db: AsyncSession, *, payload: ModelPriceIn) -> ModelPrice:
    row = ModelPrice(**payload.model_dump())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_price(
    db: AsyncSession, *, price_id: uuid.UUID, payload: ModelPriceIn
) -> ModelPrice | None:
    row = (
        await db.execute(select(ModelPrice).where(ModelPrice.id == price_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_price(db: AsyncSession, *, price_id: uuid.UUID) -> bool:
    row = (
        await db.execute(select(ModelPrice).where(ModelPrice.id == price_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
