from __future__ import annotations

from fastapi import APIRouter

from litellm.schemas import HealthResponse
from litellm.settings import settings

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(app=settings.app_name, env=settings.app_env)
