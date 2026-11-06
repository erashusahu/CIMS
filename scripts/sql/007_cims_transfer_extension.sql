BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the existing Inventroy database';
    END IF;
    IF to_regclass('cims.transfer_operation') IS NOT NULL THEN
        RAISE EXCEPTION 'cims.transfer_operation already exists; inspect it before applying 007';
    END IF;
    IF to_regclass('cims.transfer_order') IS NULL OR to_regclass('cims.transfer_order_line') IS NULL
       OR to_regclass('cims.outbox_event') IS NULL THEN
        RAISE EXCEPTION 'The transfer or inventory foundation is missing';
    END IF;
END;
$preflight$;

-- Apply after 003–006. Existing transfer_order and transfer_order_line are reused.
ALTER TABLE cims.transfer_order_line
    ADD CONSTRAINT transfer_order_line_tenant_id_line_id_key
    UNIQUE (tenant_id, transfer_order_line_id);
CREATE TABLE cims.transfer_operation (
    transfer_operation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    transfer_order_id uuid NOT NULL,
    transfer_order_line_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('DISPATCH', 'RECEIVE', 'LOSS')),
    quantity numeric(18,4) NOT NULL CHECK (quantity > 0),
    unit_cost numeric(20,6) NOT NULL CHECK (unit_cost >= 0),
    source_fifo_layer_id bigint,
    transit_fifo_layer_id bigint,
    destination_fifo_layer_id bigint,
    source_operation_id uuid,
    ledger_entry_id bigint NOT NULL,
    ledger_entry_at timestamptz NOT NULL,
    reason text,
    approved_by uuid,
    created_by uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, transfer_order_id)
        REFERENCES cims.transfer_order (tenant_id, transfer_order_id),
    FOREIGN KEY (tenant_id, transfer_order_line_id)
        REFERENCES cims.transfer_order_line (tenant_id, transfer_order_line_id),
    FOREIGN KEY (source_operation_id) REFERENCES cims.transfer_operation (transfer_operation_id)
);
ALTER TABLE cims.transfer_operation ADD UNIQUE (tenant_id, transfer_operation_id);
CREATE INDEX transfer_operation_order_idx ON cims.transfer_operation
    (tenant_id, transfer_order_id, transfer_order_line_id, action, created_at);

ALTER TABLE cims.transfer_operation ENABLE ROW LEVEL SECURITY;
ALTER TABLE cims.transfer_operation FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cims.transfer_operation
    USING (tenant_id = cims.current_tenant_id())
    WITH CHECK (tenant_id = cims.current_tenant_id());
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON cims.transfer_operation
    FOR EACH ROW EXECUTE FUNCTION cims.audit_row_change('transfer_operation_id');

CREATE FUNCTION cims.guard_transfer_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'posted transfer operations are immutable' USING ERRCODE = '23514';
END $$;
CREATE TRIGGER trg_guard_transfer_operation BEFORE UPDATE OR DELETE ON cims.transfer_operation
    FOR EACH ROW EXECUTE FUNCTION cims.guard_transfer_operation();

CREATE FUNCTION cims.guard_transfer_order_line() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO v_status FROM cims.transfer_order
          WHERE tenant_id = OLD.tenant_id AND transfer_order_id = OLD.transfer_order_id;
        IF v_status <> 'DRAFT' THEN
            RAISE EXCEPTION 'confirmed transfer lines cannot be deleted' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    SELECT status INTO v_status FROM cims.transfer_order
      WHERE tenant_id = NEW.tenant_id AND transfer_order_id = NEW.transfer_order_id;
    IF TG_OP = 'INSERT' THEN
        IF v_status <> 'DRAFT' THEN
            RAISE EXCEPTION 'confirmed transfer lines cannot be added' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF v_status <> 'DRAFT' AND (
        NEW.tenant_id, NEW.transfer_order_id, NEW.line_no, NEW.item_id,
        NEW.batch_id, NEW.uom_id, NEW.requested_quantity, NEW.quantity_in_stock_uom
    ) IS DISTINCT FROM (
        OLD.tenant_id, OLD.transfer_order_id, OLD.line_no, OLD.item_id,
        OLD.batch_id, OLD.uom_id, OLD.requested_quantity, OLD.quantity_in_stock_uom
    ) THEN
        RAISE EXCEPTION 'confirmed transfer line snapshots cannot change' USING ERRCODE = '23514';
    END IF;
    IF NEW.dispatched_quantity < OLD.dispatched_quantity
       OR NEW.received_quantity < OLD.received_quantity
       OR NEW.short_quantity < OLD.short_quantity
       OR NEW.damaged_quantity < OLD.damaged_quantity THEN
        RAISE EXCEPTION 'transfer progress cannot decrease' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_guard_transfer_order_line BEFORE INSERT OR UPDATE OR DELETE ON cims.transfer_order_line
    FOR EACH ROW EXECUTE FUNCTION cims.guard_transfer_order_line();

CREATE FUNCTION cims.guard_transfer_order() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'transfer orders cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF OLD.status <> 'DRAFT' AND (
        NEW.tenant_id, NEW.transfer_no, NEW.from_location_id, NEW.to_location_id,
        NEW.in_transit_location_id, NEW.expected_arrival_date, NEW.requested_by,
        NEW.created_by, NEW.created_at
    ) IS DISTINCT FROM (
        OLD.tenant_id, OLD.transfer_no, OLD.from_location_id, OLD.to_location_id,
        OLD.in_transit_location_id, OLD.expected_arrival_date, OLD.requested_by,
        OLD.created_by, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'confirmed transfer details are immutable' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'DRAFT' AND NEW.status = 'CONFIRMED') OR
        (OLD.status = 'CONFIRMED' AND NEW.status = 'IN_TRANSIT') OR
        (OLD.status = 'IN_TRANSIT' AND NEW.status IN ('PARTIALLY_RECEIVED', 'RECEIVED', 'SETTLED')) OR
        (OLD.status = 'PARTIALLY_RECEIVED' AND NEW.status IN ('RECEIVED', 'SETTLED'))
    ) THEN
        RAISE EXCEPTION 'invalid transfer status transition' USING ERRCODE = '23514';
    END IF;
    IF OLD.has_variance AND NOT NEW.has_variance THEN
        RAISE EXCEPTION 'transfer variance evidence cannot be cleared' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_guard_transfer_order BEFORE UPDATE OR DELETE ON cims.transfer_order
    FOR EACH ROW EXECUTE FUNCTION cims.guard_transfer_order();

COMMIT;
