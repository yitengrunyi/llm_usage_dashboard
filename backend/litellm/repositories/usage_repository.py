from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.models import TokenUsageEvent


async def insert_usage_event(db: AsyncSession, *, event: TokenUsageEvent) -> TokenUsageEvent:
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def sum_tokens_total(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> int:
    stmt = (
        select(func.coalesce(func.sum(TokenUsageEvent.tokens_total), 0))
        .where(TokenUsageEvent.tenant_id == tenant_id)
        .where(TokenUsageEvent.occurred_at >= start)
        .where(TokenUsageEvent.occurred_at < end)
    )
    return int((await db.execute(stmt)).scalar_one())
