"""ingest 子包配置. 复用 .env 里 LiteLLM 已有的 DB_* 连接参数, 仅 schema 隔离."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

# 6 张表全部建在这个 schema 下, 跟 public / llm_eva 物理隔离
DB_SCHEMA = os.environ.get("BILLING_DB_SCHEMA", "llm_usage_dashboard")

# Alembic version 表 — 用独立名字, 避免跟 LiteLLM 的 alembic_version 冲突
ALEMBIC_VERSION_TABLE = "alembic_version_ingest"


def database_url_sync() -> str:
    """alembic + sync session 用. SQLAlchemy + psycopg2 driver."""
    return (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
