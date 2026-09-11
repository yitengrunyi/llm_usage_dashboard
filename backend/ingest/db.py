"""ingest 子包数据库连接 + Base. 所有 ORM 表自动落到 llm_usage_dashboard schema."""
from __future__ import annotations

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from ingest.settings import DB_SCHEMA, database_url_sync

# 锁死 schema — 所有继承 Base 的 ORM 表 CREATE/DROP 自动带 'llm_usage_dashboard.' 前缀.
# 物理上不可能误操作其他 schema 的表.
metadata = MetaData(schema=DB_SCHEMA)


class Base(DeclarativeBase):
    metadata = metadata


engine = create_engine(
    database_url_sync(),
    pool_pre_ping=True,
    # PG 服务器 max_connections=100 但是共享给多个应用 (有 jdbc / 别的工具占着 60+ idle),
    # 留给我们的 slot 只有 ~30. 配小池, 主动 recycle, 抢不到就快速失败让上层重试.
    pool_size=3,
    max_overflow=2,        # 总上限 5 / engine / worker
    pool_recycle=300,      # 5min recycle, 别长 hold
    pool_timeout=10,       # 拿不到 connection 10s 抛 OperationalError, 不死等
    # 跨表查询不用写 schema 前缀
    connect_args={"options": f"-csearch_path={DB_SCHEMA},public"},
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
