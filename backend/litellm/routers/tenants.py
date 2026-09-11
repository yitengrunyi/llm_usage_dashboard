from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import get_db
from litellm.schemas import TenantCreateIn, TenantOut
from litellm.security import RequireApiKey
from litellm.services import tenant_service

router = APIRouter(dependencies=[RequireApiKey])


@router.post("/tenants", response_model=TenantOut)
async def create_tenant(payload: TenantCreateIn, db: AsyncSession = Depends(get_db)) -> TenantOut:
    return await tenant_service.create_tenant(db, payload=payload)


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(db: AsyncSession = Depends(get_db)) -> list[TenantOut]:
    return await tenant_service.list_tenants(db)
