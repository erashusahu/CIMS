-- Phase 3C: immutable confirmed sales details and kit-component reservation snapshots.
-- Apply to Inventroy after 003 and 004. Structure only; no business records are changed.
BEGIN;

CREATE OR REPLACE FUNCTION cims.guard_sales_order_after_confirmation()
RETURNS trigger LANGUAGE plpgsql SET search_path = cims, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.status <> 'DRAFT' THEN
        RAISE EXCEPTION 'confirmed sales orders cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status <> 'DRAFT' AND
       (OLD.tenant_id, OLD.customer_id, OLD.location_id, OLD.order_no,
        OLD.order_date, OLD.subtotal, OLD.discount_total, OLD.tax_total,
        OLD.round_off, OLD.grand_total) IS DISTINCT FROM
       (NEW.tenant_id, NEW.customer_id, NEW.location_id, NEW.order_no,
        NEW.order_date, NEW.subtotal, NEW.discount_total, NEW.tax_total,
        NEW.round_off, NEW.grand_total) THEN
        RAISE EXCEPTION 'confirmed sales order details are immutable' USING ERRCODE = '55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_sales_order_confirmed ON cims.sales_order;
CREATE TRIGGER trg_guard_sales_order_confirmed
BEFORE UPDATE OR DELETE ON cims.sales_order
FOR EACH ROW EXECUTE FUNCTION cims.guard_sales_order_after_confirmation();

CREATE OR REPLACE FUNCTION cims.guard_sales_order_line_after_confirmation()
RETURNS trigger LANGUAGE plpgsql SET search_path = cims, pg_temp AS $$
DECLARE v_status text;
        v_tenant_id uuid;
        v_order_id uuid;
BEGIN
    v_tenant_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END;
    v_order_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.sales_order_id ELSE NEW.sales_order_id END;
    SELECT status INTO v_status FROM cims.sales_order
     WHERE tenant_id = v_tenant_id AND sales_order_id = v_order_id;
    IF v_status <> 'DRAFT' THEN
        IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'confirmed sales order lines cannot be inserted or deleted'
                USING ERRCODE = '55000';
        END IF;
        IF (OLD.tenant_id, OLD.sales_order_id, OLD.line_no, OLD.item_id, OLD.uom_id,
            OLD.quantity, OLD.quantity_in_stock_uom, OLD.unit_price,
            OLD.discount_pct, OLD.discount_amount, OLD.tax_rate, OLD.taxable_value,
            OLD.tax_amount, OLD.line_total, OLD.location_id) IS DISTINCT FROM
           (NEW.tenant_id, NEW.sales_order_id, NEW.line_no, NEW.item_id, NEW.uom_id,
            NEW.quantity, NEW.quantity_in_stock_uom, NEW.unit_price,
            NEW.discount_pct, NEW.discount_amount, NEW.tax_rate, NEW.taxable_value,
            NEW.tax_amount, NEW.line_total, NEW.location_id) THEN
            RAISE EXCEPTION 'confirmed sales order line details are immutable'
                USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_sales_order_line_confirmed ON cims.sales_order_line;
CREATE TRIGGER trg_guard_sales_order_line_confirmed
BEFORE INSERT OR UPDATE OR DELETE ON cims.sales_order_line
FOR EACH ROW EXECUTE FUNCTION cims.guard_sales_order_line_after_confirmation();

CREATE OR REPLACE FUNCTION cims.guard_reservation_snapshot()
RETURNS trigger LANGUAGE plpgsql SET search_path = cims, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'reservation snapshots cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND
       (OLD.tenant_id, OLD.item_id, OLD.location_id, OLD.sales_order_id,
        OLD.sales_order_line_id, OLD.quantity) IS DISTINCT FROM
       (NEW.tenant_id, NEW.item_id, NEW.location_id, NEW.sales_order_id,
        NEW.sales_order_line_id, NEW.quantity) THEN
        RAISE EXCEPTION 'reservation snapshot details are immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_reservation_snapshot ON cims.reservation;
CREATE TRIGGER trg_guard_reservation_snapshot
BEFORE UPDATE OR DELETE ON cims.reservation
FOR EACH ROW EXECUTE FUNCTION cims.guard_reservation_snapshot();

COMMIT;
