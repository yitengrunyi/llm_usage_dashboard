from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from litellm.routers.analytics import router as analytics_router
from litellm.routers.billing import router as billing_router
from litellm.routers.health import router as health_router
from litellm.routers.model_prices import router as model_prices_router
from litellm.routers.tenants import router as tenants_router
from litellm.routers.usage import router as usage_router
from litellm.settings import settings

logger = logging.getLogger(__name__)


def _run_migrations() -> None:
    """启动时自动执行 alembic upgrade head，确保表结构存在。"""
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.warning("Alembic migration failed (tables may already exist): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_migrations()
    from litellm.services.price_persistence import seed_from_file_or_builtin
    await seed_from_file_or_builtin()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# 配置 CORS，允许前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境建议改为具体的前端域名，如 ["http://localhost:5173"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(tenants_router)
app.include_router(usage_router)
app.include_router(billing_router)
app.include_router(analytics_router)
app.include_router(model_prices_router)
