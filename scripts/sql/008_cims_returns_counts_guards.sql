-- Stage 3G physical returns and counts. Run after 003–007 in Inventroy.
-- Reuses existing return, count and adjustment tables. No business rows are changed.
BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the Inventroy database';
    END IF;
    IF to_regclass('cims.audit_count') IS NULL OR to_regclass('cims.audit_count_line') IS NULL
       OR to_regclass('cims.sales_return') IS NULL OR to_regclass('cims.adjustment') IS NULL THEN
        RAISE EXCEPTION 'The return, count or adjustment foundation is missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='cims.stock_ledger_entry'::regclass
               AND tgname='trg_count_freeze' AND NOT tgisinternal) THEN
        RAISE EXCEPTION 'The count freeze guard already exists; inspect before reapplying';
    END IF;
END;
$preflight$;

-- The same advisory lock is acquired when a count starts. This closes the race
-- between a stock posting and the count snapshot, including direct ledger INSERTs.
CREATE FUNCTION cims.enforce_count_freeze() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
DECLARE v_count_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'cims.count.freeze:' || NEW.tenant_id::text || ':' || NEW.location_id::text, 0));
    SELECT c.audit_count_id INTO v_count_id
      FROM cims.audit_count c
     WHERE c.tenant_id = NEW.tenant_id
       AND c.location_id = NEW.location_id
       AND c.status IN ('IN_PROGRESS', 'SUBMITTED', 'APPROVED')
       AND (c.freeze_location OR EXISTS (
            SELECT 1 FROM cims.audit_count_line l
             WHERE l.tenant_id = c.tenant_id AND l.audit_count_id = c.audit_count_id
               AND l.item_id = NEW.item_id))
     LIMIT 1;
    IF v_count_id IS NOT NULL AND NOT (
        NEW.source_document_type = 'ADJUSTMENT'
        AND EXISTS (
            SELECT 1 FROM cims.audit_count_line l
             WHERE l.tenant_id = NEW.tenant_id AND l.audit_count_id = v_count_id
               AND l.item_id = NEW.item_id AND l.adjustment_id = NEW.source_document_id
               AND (SELECT status FROM cims.audit_count c
                     WHERE c.tenant_id=l.tenant_id AND c.audit_count_id=l.audit_count_id) = 'APPROVED')) THEN
        RAISE EXCEPTION 'stock is frozen for an active count at this location'
            USING ERRCODE='23514', CONSTRAINT='count_scope_freeze';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_count_freeze BEFORE INSERT ON cims.stock_ledger_entry
    FOR EACH ROW EXECUTE FUNCTION cims.enforce_count_freeze();

CREATE FUNCTION cims.guard_count_document() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'count documents cannot be deleted' USING ERRCODE='23514';
    END IF;
    IF OLD.status <> 'DRAFT' AND (
        NEW.tenant_id, NEW.count_no, NEW.location_id, NEW.count_type,
        NEW.abc_class, NEW.blind_count, NEW.freeze_location, NEW.snapshot_at,
        NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM (
        OLD.tenant_id, OLD.count_no, OLD.location_id, OLD.count_type,
        OLD.abc_class, OLD.blind_count, OLD.freeze_location, OLD.snapshot_at,
        OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'count scope and snapshot are immutable after start' USING ERRCODE='23514';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status='DRAFT' AND NEW.status IN ('IN_PROGRESS','CANCELLED')) OR
        (OLD.status='IN_PROGRESS' AND NEW.status IN ('SUBMITTED','CANCELLED')) OR
        (OLD.status='SUBMITTED' AND NEW.status IN ('APPROVED','CANCELLED')) OR
        (OLD.status='APPROVED' AND NEW.status IN ('POSTED','CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'invalid count status transition' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_guard_count_document BEFORE UPDATE OR DELETE ON cims.audit_count
    FOR EACH ROW EXECUTE FUNCTION cims.guard_count_document();

CREATE FUNCTION cims.guard_count_line() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
DECLARE v_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO v_status FROM cims.audit_count
         WHERE tenant_id=OLD.tenant_id AND audit_count_id=OLD.audit_count_id;
        IF v_status <> 'DRAFT' THEN
            RAISE EXCEPTION 'started count lines cannot be deleted' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    SELECT status INTO v_status FROM cims.audit_count
     WHERE tenant_id=NEW.tenant_id AND audit_count_id=NEW.audit_count_id;
    IF TG_OP = 'INSERT' THEN
        IF v_status <> 'DRAFT' THEN
            RAISE EXCEPTION 'started count scope cannot grow' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF v_status IN ('POSTED','CANCELLED') THEN
        RAISE EXCEPTION 'closed count lines are immutable' USING ERRCODE='23514';
    END IF;
    IF v_status <> 'DRAFT' AND (
        NEW.tenant_id, NEW.audit_count_id, NEW.item_id, NEW.batch_id, NEW.system_quantity
    ) IS DISTINCT FROM (
        OLD.tenant_id, OLD.audit_count_id, OLD.item_id, OLD.batch_id, OLD.system_quantity
    ) THEN
        RAISE EXCEPTION 'count scope and snapshot cannot change' USING ERRCODE='23514';
    END IF;
    IF v_status NOT IN ('DRAFT','IN_PROGRESS') AND (
        NEW.counted_quantity, NEW.recount_quantity
    ) IS DISTINCT FROM (
        OLD.counted_quantity, OLD.recount_quantity
    ) THEN
        RAISE EXCEPTION 'submitted count quantities cannot change' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_guard_count_line BEFORE INSERT OR UPDATE OR DELETE ON cims.audit_count_line
    FOR EACH ROW EXECUTE FUNCTION cims.guard_count_line();

CREATE FUNCTION cims.guard_received_return() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
DECLARE v_status text;
BEGIN
    IF TG_TABLE_NAME = 'sales_return' THEN
        IF TG_OP = 'DELETE' OR OLD.status IN ('RECEIVED','CLOSED','CANCELLED') THEN
            RAISE EXCEPTION 'received returns are immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO v_status FROM cims.sales_return
         WHERE tenant_id=OLD.tenant_id AND sales_return_id=OLD.sales_return_id;
        IF v_status <> 'DRAFT' THEN
            RAISE EXCEPTION 'approved return lines cannot be removed' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    SELECT status INTO v_status FROM cims.sales_return
     WHERE tenant_id=NEW.tenant_id AND sales_return_id=NEW.sales_return_id;
    IF v_status <> 'DRAFT' THEN
        RAISE EXCEPTION 'approved return lines are immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_guard_received_return BEFORE UPDATE OR DELETE ON cims.sales_return
    FOR EACH ROW EXECUTE FUNCTION cims.guard_received_return();
CREATE TRIGGER trg_guard_received_return_line BEFORE INSERT OR UPDATE OR DELETE ON cims.sales_return_line
    FOR EACH ROW EXECUTE FUNCTION cims.guard_received_return();

COMMIT;
