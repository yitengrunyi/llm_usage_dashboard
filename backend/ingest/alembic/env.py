"""Alembic env — 关键防护:
1. 只看 llm_usage_dashboard schema 的表 (include_object 过滤)
2. version 表名 alembic_version_ingest, 跟 LiteLLM 的 alembic_version 完全不冲突
3. autogenerate 永远不会想去 DROP public / llm_eva 里的表 (那些不在我们 MetaData 里)
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ingest.db import Base
from ingest.settings import ALEMBIC_VERSION_TABLE, DB_SCHEMA, database_url_sync
import ingest.models  # noqa: F401 — 让 Base.metadata 看到所有表

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    """schema 级过滤: autogenerate 反射 DB 时只看 llm_usage_dashboard, 别的 schema 整个跳过.
    没这行的话 autogenerate 会反射 public 里 LiteLLM 等几十张表, 想 DROP 它们.
    """
    if type_ == "schema":
        return name == DB_SCHEMA
    return True


def include_object(obj, name, type_, reflected, compare_to):
    """对象级过滤: 双保险. 任何不在 llm_usage_dashboard schema 的对象一律忽略."""
    obj_schema = getattr(obj, "schema", None)
    if reflected and obj_schema != DB_SCHEMA:
        return False
    return True


def get_url() -> str:
    return database_url_sync()


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
        version_table=ALEMBIC_VERSION_TABLE,
        version_table_schema=DB_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_schemas=True,
            include_name=include_name,
            include_object=include_object,
            version_table=ALEMBIC_VERSION_TABLE,
            version_table_schema=DB_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
