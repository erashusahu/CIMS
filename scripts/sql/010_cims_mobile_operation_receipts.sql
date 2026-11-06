-- Stage 3I: accepted mobile operation receipts. Run after 003–009 in Inventroy.
-- Business operations still use their existing domain tables and services.
BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the Inventroy database';
    END IF;
    IF to_regclass('cims.mobile_operation_receipt') IS NOT NULL THEN
        RAISE EXCEPTION 'mobile_operation_receipt already exists; inspect before reapplying';
    END IF;
    IF to_regclass('cims.tenant') IS NULL OR to_regclass('cims.audit_log') IS NULL THEN
        RAISE EXCEPTION 'The tenant or audit foundation is missing';
    END IF;
END;
$preflight$;

CREATE TABLE cims.mobile_operation_receipt (
    operation_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    actor_key text NOT NULL,
    operation_kind text NOT NULL CHECK (operation_kind IN (
        'RECEIPT_DRAFT', 'PICK_SCAN', 'SHIPMENT_PACK',
        'TRANSFER_RECEIVE', 'COUNT_SUBMIT', 'POS_DRAFT')),
    document_id uuid,
    request_hash text NOT NULL,
    captured_at timestamptz NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    result jsonb NOT NULL,
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id) REFERENCES cims.tenant(tenant_id) ON DELETE CASCADE
);
CREATE INDEX mobile_receipt_actor_idx ON cims.mobile_operation_receipt
    (tenant_id, actor_key, accepted_at DESC);
ALTER TABLE cims.mobile_operation_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE cims.mobile_operation_receipt FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cims.mobile_operation_receipt
    USING (tenant_id = cims.current_tenant_id())
    WITH CHECK (tenant_id = cims.current_tenant_id());
CREATE TRIGGER trg_audit AFTER INSERT ON cims.mobile_operation_receipt
    FOR EACH ROW EXECUTE FUNCTION cims.audit_row_change('operation_id');

CREATE FUNCTION cims.guard_mobile_receipt() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
BEGIN
    RAISE EXCEPTION 'accepted mobile receipts are immutable' USING ERRCODE='23514';
END;
$guard$;
CREATE TRIGGER trg_guard_mobile_receipt BEFORE UPDATE OR DELETE ON cims.mobile_operation_receipt
    FOR EACH ROW EXECUTE FUNCTION cims.guard_mobile_receipt();
COMMIT;
