from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.models import Tenant


async def create_tenant(db: AsyncSession, *, name: str) -> Tenant:
    row = Tenant(name=name)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_tenants(db: AsyncSession) -> list[Tenant]:
    rows = (await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))).scalars().all()
    return list(rows)
