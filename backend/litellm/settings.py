from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="llm-usage-dashboard", alias="APP_NAME")
    app_env: str = Field(default="local", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 无默认值: 未配置 API_KEY 时 /api/litellm/* 一律 401 (fail-closed, 见 security.py)
    api_key: str = Field(default="", alias="API_KEY")

    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(default="token_billing", alias="DB_NAME")
    db_user: str = Field(default="token_billing", alias="DB_USER")
    db_password: str = Field(default="token_billing", alias="DB_PASSWORD")

    price_per_1k_tokens: float = Field(default=0.02, alias="PRICE_PER_1K_TOKENS")

    @property
    def database_url_async(self) -> str:
        # SQLAlchemy async URL
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def database_url_sync(self) -> str:
        # Alembic uses sync engine
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()

