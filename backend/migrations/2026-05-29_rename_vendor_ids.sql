-- 一次性 rename: gptmeta → blueshirt, xhub → nulls
-- 跟前端展示名对齐. 在服务器 PG 上跑一次, 跑完代码 rebuild.
--
-- 步骤:
-- 1. drop + 重建 FK 加 ON UPDATE CASCADE (后续 rename 不疼)
-- 2. UPDATE vendor_meta PK — CASCADE 自动传到 daily / model / state
-- 3. UPDATE vendor_ingest_run (没 FK 引用 vendor_meta, 单独改)
--
-- 服务器执行:
--   docker exec -i $(sudo docker compose ps -q backend) python3 -c "
--     import sys; sys.path.insert(0, '.')
--     from ingest.db import SessionLocal
--     from sqlalchemy import text
--     with SessionLocal() as s:
--         s.execute(text(open('migrations/2026-05-29_rename_vendor_ids.sql').read()))
--         s.commit()"
-- 或者用 psql 直接连 PG 执行:
--   PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U postgres -d postgres -c "SET search_path=llm_usage_dashboard,public;" -f migrations/2026-05-29_rename_vendor_ids.sql

BEGIN;

-- 1. FK ON UPDATE CASCADE
ALTER TABLE vendor_usage_daily
    DROP CONSTRAINT vendor_usage_daily_vendor_id_fkey,
    ADD CONSTRAINT vendor_usage_daily_vendor_id_fkey
        FOREIGN KEY (vendor_id) REFERENCES vendor_meta(vendor_id) ON UPDATE CASCADE;

ALTER TABLE vendor_model_usage_daily
    DROP CONSTRAINT vendor_model_usage_daily_vendor_id_usage_date_fkey,
    ADD CONSTRAINT vendor_model_usage_daily_vendor_id_usage_date_fkey
        FOREIGN KEY (vendor_id, usage_date)
        REFERENCES vendor_usage_daily(vendor_id, usage_date) ON UPDATE CASCADE;

ALTER TABLE vendor_ingest_state
    DROP CONSTRAINT vendor_ingest_state_vendor_id_fkey,
    ADD CONSTRAINT vendor_ingest_state_vendor_id_fkey
        FOREIGN KEY (vendor_id) REFERENCES vendor_meta(vendor_id) ON UPDATE CASCADE;

-- 2. rename PK (自动级联到 daily / model / state)
UPDATE vendor_meta SET vendor_id='blueshirt' WHERE vendor_id='gptmeta';
UPDATE vendor_meta SET vendor_id='nulls'     WHERE vendor_id='xhub';

-- 3. vendor_ingest_run (没 FK)
UPDATE vendor_ingest_run SET vendor_id='blueshirt' WHERE vendor_id='gptmeta';
UPDATE vendor_ingest_run SET vendor_id='nulls'     WHERE vendor_id='xhub';

COMMIT;

-- verify
SELECT vendor_id, COUNT(*) AS days FROM vendor_usage_daily GROUP BY vendor_id ORDER BY 1;
