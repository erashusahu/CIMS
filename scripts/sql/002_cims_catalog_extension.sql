-- Chunk 2 additions to the existing cims.item table in Inventroy.
-- Prepared 2026-11-01. For the user to apply; never run automatically by the app.
-- Authentication extension 001 is not required for the local catalog workspace.
-- No existing business records are edited and no stock movements are created.
BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the existing Inventroy database';
    END IF;
    IF to_regclass('cims.item') IS NULL OR to_regclass('cims.cost_center') IS NULL THEN
        RAISE EXCEPTION 'The existing CIMS catalog tables are required';
    END IF;
END;
$preflight$;

ALTER TABLE cims.item
    ADD COLUMN catalog_version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN cost_center_id UUID,
    ADD CONSTRAINT item_catalog_version_positive CHECK (catalog_version > 0),
    ADD CONSTRAINT item_catalog_cost_center_fk
        FOREIGN KEY (tenant_id, cost_center_id)
        REFERENCES cims.cost_center (tenant_id, cost_center_id);

COMMIT;
