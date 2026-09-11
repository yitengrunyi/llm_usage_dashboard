from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from litellm.settings import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url_async,
    pool_pre_ping=True,
    # 跟 ingest/db.py 同步: PG 共享池紧张, 配小 + 快速 recycle
    pool_size=3,
    max_overflow=2,
    pool_recycle=300,
    pool_timeout=10,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

