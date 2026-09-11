from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_400_BAD_REQUEST

from litellm.repositories import tenant_repository
from litellm.schemas import TenantCreateIn, TenantOut


async def create_tenant(db: AsyncSession, *, payload: TenantCreateIn) -> TenantOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="name is required")
    row = await tenant_repository.create_tenant(db, name=name)
    return TenantOut(id=row.id, name=row.name, created_at=row.created_at)


async def list_tenants(db: AsyncSession) -> list[TenantOut]:
    rows = await tenant_repository.list_tenants(db)
    return [TenantOut(id=r.id, name=r.name, created_at=r.created_at) for r in rows]
