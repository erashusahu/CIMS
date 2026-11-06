-- Phase 3B: keep posted purchase-receipt lines immutable.
-- Apply in pgAdmin to Inventroy after 003_cims_opening_stock.sql.
-- This script changes structure only; it does not edit business records.
BEGIN;

CREATE OR REPLACE FUNCTION cims.guard_purchase_receive_line()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = cims, pg_temp
AS $$
DECLARE
    v_status text;
    v_receipt_id uuid;
    v_tenant_id uuid;
BEGIN
    v_receipt_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.purchase_receive_id ELSE NEW.purchase_receive_id END;
    v_tenant_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    SELECT status INTO v_status
      FROM cims.purchase_receive
     WHERE tenant_id = v_tenant_id AND purchase_receive_id = v_receipt_id;
    IF v_status = 'POSTED' THEN
        RAISE EXCEPTION 'posted purchase receipt lines are immutable' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND
       (OLD.tenant_id, OLD.purchase_receive_id) IS DISTINCT FROM
       (NEW.tenant_id, NEW.purchase_receive_id) THEN
        RAISE EXCEPTION 'receipt line ownership cannot change' USING ERRCODE = '55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_purchase_receive_line ON cims.purchase_receive_line;
CREATE TRIGGER trg_guard_purchase_receive_line
BEFORE INSERT OR UPDATE OR DELETE ON cims.purchase_receive_line
FOR EACH ROW EXECUTE FUNCTION cims.guard_purchase_receive_line();

COMMIT;
