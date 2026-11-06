-- Stage 3H POS preparation. Run after 003–008 in Inventroy.
-- Checkout remains unavailable until an atomic billing/payment interface exists.
BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the Inventroy database';
    END IF;
    IF to_regclass('cims.sales_order') IS NULL THEN
        RAISE EXCEPTION 'The sales order foundation is missing';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='cims' AND table_name='sales_order'
                 AND column_name='sales_source') THEN
        RAISE EXCEPTION 'sales_source already exists; inspect before reapplying';
    END IF;
END;
$preflight$;

ALTER TABLE cims.sales_order
    ADD COLUMN sales_source text NOT NULL DEFAULT 'MANUAL_RETAIL',
    ADD CONSTRAINT sales_order_source_check
        CHECK (sales_source IN ('MANUAL_RETAIL', 'WHOLESALE', 'POS')),
    ADD CONSTRAINT pos_draft_only_check
        CHECK (sales_source <> 'POS' OR status IN ('DRAFT', 'CANCELLED'));
CREATE INDEX sales_order_source_idx ON cims.sales_order
    (tenant_id, sales_source, created_at DESC);

CREATE FUNCTION cims.guard_sales_source() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
BEGIN
    IF NEW.sales_source IS DISTINCT FROM OLD.sales_source THEN
        RAISE EXCEPTION 'sales source cannot change after creation'
            USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_guard_sales_source BEFORE UPDATE ON cims.sales_order
    FOR EACH ROW EXECUTE FUNCTION cims.guard_sales_source();
COMMIT;
