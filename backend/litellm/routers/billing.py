from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from litellm.db import get_db
from litellm.schemas import BillingSummaryOut
from litellm.security import RequireApiKey
from litellm.services import billing_service

router = APIRouter(dependencies=[RequireApiKey])


@router.get("/billing/summary", response_model=BillingSummaryOut)
async def billing_summary(
    tenant_id: uuid.UUID = Query(..., description="Tenant UUID"),
    start: datetime = Query(..., description="ISO8601, e.g. 2026-02-01T00:00:00Z"),
    end: datetime = Query(..., description="ISO8601, e.g. 2026-02-29T23:59:59Z"),
    db: AsyncSession = Depends(get_db),
) -> BillingSummaryOut:
    return await billing_service.billing_summary(db, tenant_id=tenant_id, start=start, end=end)
