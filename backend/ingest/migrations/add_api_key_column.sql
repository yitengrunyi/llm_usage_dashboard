-- vendor_apikey_usage_daily: 按 (vendor, day, api_key, model) 拆分的平行明细表.
-- 只有上游能按 API key (token_name) 拆分的 vendor 才写 (目前仅 apevon).
-- 主表 vendor_model_usage_daily / vendor_usage_daily 完全不动.
-- 等价于 alembic migration 20260811_0002; 此文件供 docker/手动 psql 直接跑.

CREATE TABLE IF NOT EXISTS llm_usage_dashboard.vendor_apikey_usage_daily (
    vendor_id        VARCHAR        NOT NULL,
    usage_date       DATE           NOT NULL,
    api_key          VARCHAR(255)   NOT NULL,
    model            VARCHAR        NOT NULL,
    prompt_tokens    BIGINT,
    completion_tokens BIGINT,
    cache_read_tokens BIGINT,
    cache_write_tokens BIGINT,
    total_tokens     BIGINT,
    request_count    INTEGER,
    image_count      INTEGER,
    cost_native      NUMERIC(18, 8) NOT NULL,
    cost_usd         NUMERIC(18, 8) NOT NULL,
    cost_cny         NUMERIC(18, 8) NOT NULL,
    fx_rate          NUMERIC(12, 6),
    last_run_id      BIGINT REFERENCES llm_usage_dashboard.vendor_ingest_run(id),
    ingested_at      TIMESTAMPTZ DEFAULT now() NOT NULL,
    PRIMARY KEY (vendor_id, usage_date, api_key, model)
);

CREATE INDEX IF NOT EXISTS idx_vakud_vendor_date
    ON llm_usage_dashboard.vendor_apikey_usage_daily (vendor_id, usage_date);
CREATE INDEX IF NOT EXISTS idx_vakud_vendor_key
    ON llm_usage_dashboard.vendor_apikey_usage_daily (vendor_id, api_key);
CREATE INDEX IF NOT EXISTS idx_vakud_date_model
    ON llm_usage_dashboard.vendor_apikey_usage_daily (usage_date, model);
