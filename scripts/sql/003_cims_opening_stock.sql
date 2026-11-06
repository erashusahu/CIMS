-- Chunk 3A: opening-stock documents and a transactional outbox.
-- Apply manually to Inventroy after 002_cims_catalog_extension.sql.
-- No business records are inserted or stock quantities changed by this script.
BEGIN;

DO $preflight$
BEGIN
    IF current_database() <> 'Inventroy' THEN
        RAISE EXCEPTION 'Select the existing Inventroy database';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='cims'
                   AND table_name='item' AND column_name='catalog_version') THEN
        RAISE EXCEPTION 'Apply catalog extension 002 first';
    END IF;
    IF to_regclass('cims.opening_stock') IS NOT NULL
       OR to_regclass('cims.opening_stock_line') IS NOT NULL
       OR to_regclass('cims.outbox_event') IS NOT NULL THEN
        RAISE EXCEPTION 'An opening-stock/outbox table already exists; review its definition first';
    END IF;
END;
$preflight$;

-- The new enum value is first used by application requests AFTER this transaction commits.
ALTER TYPE cims.document_kind_t ADD VALUE IF NOT EXISTS 'OPENING_STOCK';

CREATE TABLE cims.opening_stock (
    opening_stock_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES cims.tenant(tenant_id),
    document_no TEXT NOT NULL,
    business_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','POSTED','CANCELLED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    notes TEXT,
    created_by UUID,
    posted_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at TIMESTAMPTZ,
    UNIQUE (tenant_id, opening_stock_id),
    UNIQUE (tenant_id, document_no),
    FOREIGN KEY (tenant_id, created_by) REFERENCES cims.app_user(tenant_id, user_id),
    FOREIGN KEY (tenant_id, posted_by) REFERENCES cims.app_user(tenant_id, user_id),
    CHECK ((status = 'POSTED') = (posted_at IS NOT NULL))
);

CREATE TABLE cims.opening_stock_line (
    opening_stock_line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    opening_stock_id UUID NOT NULL,
    line_no INTEGER NOT NULL CHECK (line_no > 0),
    item_id UUID NOT NULL,
    location_id UUID NOT NULL,
    entered_uom_id UUID NOT NULL,
    entered_quantity NUMERIC(24,10) NOT NULL CHECK (entered_quantity > 0),
    conversion_factor NUMERIC(24,10) NOT NULL CHECK (conversion_factor > 0),
    stock_uom_id UUID NOT NULL,
    stock_quantity NUMERIC(18,4) NOT NULL CHECK (stock_quantity > 0),
    unit_cost NUMERIC(20,6) NOT NULL CHECK (unit_cost >= 0),
    item_catalog_version BIGINT NOT NULL,
    sku_snapshot TEXT NOT NULL,
    item_name_snapshot TEXT NOT NULL,
    stock_unit_snapshot TEXT NOT NULL,
    location_code_snapshot TEXT NOT NULL,
    UNIQUE (tenant_id, opening_stock_id, line_no),
    UNIQUE (tenant_id, opening_stock_id, item_id, location_id),
    FOREIGN KEY (tenant_id, opening_stock_id)
        REFERENCES cims.opening_stock(tenant_id, opening_stock_id),
    FOREIGN KEY (tenant_id, item_id) REFERENCES cims.item(tenant_id, item_id),
    FOREIGN KEY (tenant_id, location_id) REFERENCES cims.location(tenant_id, location_id),
    FOREIGN KEY (tenant_id, entered_uom_id) REFERENCES cims.uom(tenant_id, uom_id),
    FOREIGN KEY (tenant_id, stock_uom_id) REFERENCES cims.uom(tenant_id, uom_id)
);

CREATE TABLE cims.outbox_event (
    outbox_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES cims.tenant(tenant_id),
    event_type TEXT NOT NULL,
    aggregate_id UUID NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','DONE','FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    last_error_code TEXT,
    UNIQUE (tenant_id, event_type, aggregate_id)
);
CREATE INDEX ix_opening_stock_tenant_date ON cims.opening_stock(tenant_id, created_at DESC);
CREATE INDEX ix_opening_stock_line_document ON cims.opening_stock_line(tenant_id, opening_stock_id);
CREATE INDEX ix_outbox_pending ON cims.outbox_event(tenant_id, available_at, outbox_event_id)
    WHERE status IN ('PENDING','FAILED');

CREATE FUNCTION cims.protect_opening_stock() RETURNS trigger
LANGUAGE plpgsql SET search_path = cims, pg_catalog AS $function$
DECLARE parent_status TEXT;
BEGIN
    IF TG_TABLE_NAME = 'opening_stock' THEN
        IF TG_OP = 'DELETE' OR OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'Only draft opening-stock documents can be changed' USING ERRCODE='55000';
        END IF;
        IF NEW.tenant_id <> OLD.tenant_id OR NEW.opening_stock_id <> OLD.opening_stock_id
           OR NEW.document_no <> OLD.document_no THEN
            RAISE EXCEPTION 'Opening-stock identity is immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP IN ('UPDATE','DELETE') THEN
        SELECT status INTO parent_status FROM cims.opening_stock
            WHERE tenant_id=OLD.tenant_id AND opening_stock_id=OLD.opening_stock_id FOR UPDATE;
        IF parent_status IS DISTINCT FROM 'DRAFT' THEN
            RAISE EXCEPTION 'Posted or cancelled opening-stock lines are immutable' USING ERRCODE='55000';
        END IF;
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        SELECT status INTO parent_status FROM cims.opening_stock
            WHERE tenant_id=NEW.tenant_id AND opening_stock_id=NEW.opening_stock_id FOR UPDATE;
        IF parent_status IS DISTINCT FROM 'DRAFT' THEN
            RAISE EXCEPTION 'Opening-stock lines require a draft document' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$function$;

CREATE TRIGGER opening_stock_immutable BEFORE UPDATE OR DELETE ON cims.opening_stock
    FOR EACH ROW EXECUTE FUNCTION cims.protect_opening_stock();
CREATE TRIGGER opening_stock_line_immutable BEFORE INSERT OR UPDATE OR DELETE ON cims.opening_stock_line
    FOR EACH ROW EXECUTE FUNCTION cims.protect_opening_stock();
CREATE TRIGGER opening_stock_no_truncate BEFORE TRUNCATE ON cims.opening_stock
    FOR EACH STATEMENT EXECUTE FUNCTION cims.deny_mutation();
CREATE TRIGGER opening_stock_line_no_truncate BEFORE TRUNCATE ON cims.opening_stock_line
    FOR EACH STATEMENT EXECUTE FUNCTION cims.deny_mutation();
CREATE TRIGGER opening_stock_audit AFTER INSERT OR UPDATE ON cims.opening_stock
    FOR EACH ROW EXECUTE FUNCTION cims.audit_row_change('opening_stock_id');
CREATE TRIGGER opening_stock_line_audit AFTER INSERT OR UPDATE OR DELETE ON cims.opening_stock_line
    FOR EACH ROW EXECUTE FUNCTION cims.audit_row_change('opening_stock_line_id');

DO $policies$
DECLARE relation_name TEXT; partition_name REGCLASS;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY['opening_stock','opening_stock_line','outbox_event'] LOOP
        EXECUTE format('ALTER TABLE cims.%I ENABLE ROW LEVEL SECURITY', relation_name);
        EXECUTE format('ALTER TABLE cims.%I FORCE ROW LEVEL SECURITY', relation_name);
        EXECUTE format('CREATE POLICY tenant_isolation ON cims.%I USING (tenant_id=cims.current_tenant_id()) WITH CHECK (tenant_id=cims.current_tenant_id())', relation_name);
        EXECUTE format('REVOKE ALL ON cims.%I FROM PUBLIC', relation_name);
    END LOOP;
    -- Row-level mutation guards do not cover TRUNCATE. Protect the parent and every
    -- current partition; future partition maintenance must install the same guard.
    FOR partition_name IN SELECT relid FROM pg_partition_tree('cims.stock_ledger_entry'::regclass) LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=partition_name AND tgname='stock_no_truncate') THEN
            EXECUTE format('CREATE TRIGGER stock_no_truncate BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION cims.deny_mutation()', partition_name);
        END IF;
    END LOOP;
END;
$policies$;

COMMIT;
