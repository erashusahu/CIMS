-- Phase 3D: scan evidence and posted-shipment guards. Apply after 003, 004, 005.
-- Structure only: no business records or stock quantities are changed.
BEGIN;
DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the existing Inventroy database';
    END IF;
    IF to_regclass('cims.pick_scan') IS NOT NULL THEN
        RAISE EXCEPTION 'cims.pick_scan already exists; inspect it before applying 006';
    END IF;
    IF to_regclass('cims.shipment') IS NULL OR to_regclass('cims.package') IS NULL
       OR to_regclass('cims.reservation') IS NULL THEN
        RAISE EXCEPTION 'The shipment, package, or reservation foundation is missing';
    END IF;
END;
$preflight$;

CREATE TABLE cims.pick_scan (
    pick_scan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    sales_order_id UUID NOT NULL,
    sales_order_line_id UUID NOT NULL,
    reservation_id UUID NOT NULL,
    item_id UUID NOT NULL,
    location_id UUID NOT NULL,
    scanned_code TEXT NOT NULL,
    quantity NUMERIC(18,4) NOT NULL CHECK (quantity > 0),
    shipped_quantity NUMERIC(18,4) NOT NULL DEFAULT 0
        CHECK (shipped_quantity >= 0 AND shipped_quantity <= quantity),
    is_override BOOLEAN NOT NULL DEFAULT false,
    override_reason TEXT,
    scanned_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, pick_scan_id),
    FOREIGN KEY (tenant_id, sales_order_id) REFERENCES cims.sales_order(tenant_id, sales_order_id),
    FOREIGN KEY (tenant_id, sales_order_line_id)
        REFERENCES cims.sales_order_line(tenant_id, sales_order_line_id),
    FOREIGN KEY (tenant_id, reservation_id)
        REFERENCES cims.reservation(tenant_id, reservation_id),
    FOREIGN KEY (tenant_id, item_id) REFERENCES cims.item(tenant_id, item_id),
    FOREIGN KEY (tenant_id, location_id) REFERENCES cims.location(tenant_id, location_id),
    FOREIGN KEY (tenant_id, scanned_by) REFERENCES cims.app_user(tenant_id, user_id),
    CHECK (is_override = (override_reason IS NOT NULL))
);
CREATE INDEX ix_pick_scan_reservation ON cims.pick_scan(tenant_id, reservation_id, created_at);
ALTER TABLE cims.pick_scan ENABLE ROW LEVEL SECURITY;
ALTER TABLE cims.pick_scan FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON cims.pick_scan
    USING (tenant_id = cims.current_tenant_id())
    WITH CHECK (tenant_id = cims.current_tenant_id());
CREATE TRIGGER trg_audit AFTER INSERT OR UPDATE OR DELETE ON cims.pick_scan
    FOR EACH ROW EXECUTE FUNCTION cims.audit_row_change('pick_scan_id');

CREATE FUNCTION cims.guard_pick_scan() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'pick scans cannot be deleted' USING ERRCODE='55000';
    END IF;
    IF (OLD.tenant_id, OLD.sales_order_id, OLD.sales_order_line_id,
        OLD.reservation_id, OLD.item_id, OLD.location_id, OLD.scanned_code,
        OLD.quantity, OLD.is_override, OLD.override_reason, OLD.scanned_by) IS DISTINCT FROM
       (NEW.tenant_id, NEW.sales_order_id, NEW.sales_order_line_id,
        NEW.reservation_id, NEW.item_id, NEW.location_id, NEW.scanned_code,
        NEW.quantity, NEW.is_override, NEW.override_reason, NEW.scanned_by) THEN
        RAISE EXCEPTION 'pick scan evidence is immutable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END;
$guard$;
CREATE TRIGGER trg_guard_pick_scan BEFORE UPDATE OR DELETE ON cims.pick_scan
    FOR EACH ROW EXECUTE FUNCTION cims.guard_pick_scan();

CREATE FUNCTION cims.guard_shipped_document() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $guard$
DECLARE parent_status TEXT;
BEGIN
    IF TG_TABLE_NAME = 'shipment' THEN
        IF TG_OP = 'DELETE' AND OLD.status = 'SHIPPED' THEN
            RAISE EXCEPTION 'shipped document cannot be deleted' USING ERRCODE='55000';
        END IF;
        IF TG_OP = 'UPDATE' AND OLD.status = 'SHIPPED' AND
           (OLD.tenant_id, OLD.shipment_no, OLD.sales_order_id, OLD.from_location_id,
            OLD.shipped_at, OLD.is_partial) IS DISTINCT FROM
           (NEW.tenant_id, NEW.shipment_no, NEW.sales_order_id, NEW.from_location_id,
            NEW.shipped_at, NEW.is_partial) THEN
            RAISE EXCEPTION 'shipped document details are immutable' USING ERRCODE='55000';
        END IF;
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_TABLE_NAME IN ('shipment_line', 'shipment_package') THEN
        SELECT status INTO parent_status FROM cims.shipment
         WHERE tenant_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.tenant_id ELSE NEW.tenant_id END
           AND shipment_id = CASE WHEN TG_OP = 'DELETE' THEN OLD.shipment_id ELSE NEW.shipment_id END;
    ELSIF TG_TABLE_NAME IN ('package', 'package_line') THEN
        SELECT s.status INTO parent_status FROM cims.shipment_package sp
        JOIN cims.shipment s ON (s.tenant_id, s.shipment_id) = (sp.tenant_id, sp.shipment_id)
        WHERE sp.tenant_id = CASE WHEN TG_OP = 'DELETE' OR TG_TABLE_NAME = 'package' THEN OLD.tenant_id ELSE NEW.tenant_id END
          AND sp.package_id = CASE WHEN TG_OP = 'DELETE' OR TG_TABLE_NAME = 'package' THEN OLD.package_id ELSE NEW.package_id END
          AND s.status = 'SHIPPED' LIMIT 1;
    END IF;
    IF parent_status = 'SHIPPED' THEN
        RAISE EXCEPTION 'shipped package and line details are immutable' USING ERRCODE='55000';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$guard$;
CREATE TRIGGER trg_guard_shipment BEFORE UPDATE OR DELETE ON cims.shipment
    FOR EACH ROW EXECUTE FUNCTION cims.guard_shipped_document();
CREATE TRIGGER trg_guard_shipment_line BEFORE INSERT OR UPDATE OR DELETE ON cims.shipment_line
    FOR EACH ROW EXECUTE FUNCTION cims.guard_shipped_document();
CREATE TRIGGER trg_guard_shipment_package BEFORE INSERT OR UPDATE OR DELETE ON cims.shipment_package
    FOR EACH ROW EXECUTE FUNCTION cims.guard_shipped_document();
CREATE TRIGGER trg_guard_package BEFORE UPDATE OR DELETE ON cims.package
    FOR EACH ROW EXECUTE FUNCTION cims.guard_shipped_document();
CREATE TRIGGER trg_guard_package_line BEFORE INSERT OR UPDATE OR DELETE ON cims.package_line
    FOR EACH ROW EXECUTE FUNCTION cims.guard_shipped_document();
COMMIT;
