from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import get_db
from litellm.repositories import model_price_repository as repo
from litellm.schemas import ModelPriceIn, ModelPriceOut
from litellm.security import RequireApiKey
from litellm.services.price_persistence import export_to_file
from litellm.services.pricing_cache_service import refresh_price_cache

router = APIRouter(
    prefix="/model-prices",
    tags=["model-prices"],
    dependencies=[RequireApiKey],
)


@router.get("", response_model=list[ModelPriceOut])
async def list_model_prices(db: AsyncSession = Depends(get_db)):
    return await repo.list_prices(db)


@router.post("", response_model=ModelPriceOut, status_code=201)
async def create_model_price(payload: ModelPriceIn, db: AsyncSession = Depends(get_db)):
    existing = await repo.get_by_model_name(db, model_name=payload.model_name, provider=payload.provider)
    if existing:
        raise HTTPException(status_code=409, detail=f"模型 {payload.model_name!r}（provider={payload.provider!r}）已存在")
    row = await repo.create_price(db, payload=payload)
    await export_to_file(db)
    await refresh_price_cache()
    return row


@router.put("/{price_id}", response_model=ModelPriceOut)
async def update_model_price(price_id: uuid.UUID, payload: ModelPriceIn, db: AsyncSession = Depends(get_db)):
    # 检查修改后的 (model_name, provider) 是否与其他记录冲突
    existing = await repo.get_by_model_name(db, model_name=payload.model_name, provider=payload.provider)
    if existing and existing.id != price_id:
        raise HTTPException(
            status_code=409,
            detail=f"模型 {payload.model_name!r}（provider={payload.provider!r}）已被其他记录占用",
        )
    row = await repo.update_price(db, price_id=price_id, payload=payload)
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该定价配置")
    await export_to_file(db)
    await refresh_price_cache()
    return row


@router.delete("/{price_id}", status_code=204)
async def delete_model_price(price_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    ok = await repo.delete_price(db, price_id=price_id)
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该定价配置")
    await export_to_file(db)
    await refresh_price_cache()


@router.post("/refresh-cache", status_code=200)
async def refresh_cache():
    """手动刷新定价缓存。"""
    await refresh_price_cache()
    return {"ok": True, "message": "定价缓存已刷新"}
