"""Core mappings for the existing cims catalog; no runtime schema creation."""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

catalog_metadata = MetaData(schema="cims")
valuation_type = ENUM(
    "FIFO", "WEIGHTED_AVERAGE", name="valuation_method_t", schema="cims", create_type=False
)


def tenant_table(name: str, key: str, *columns: Column) -> Table:
    return Table(
        name,
        catalog_metadata,
        Column(key, UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
        Column("tenant_id", UUID(as_uuid=True), nullable=False),
        *columns,
    )


units = tenant_table(
    "uom",
    "uom_id",
    Column("code", String),
    Column("name", Text),
    Column("uom_class", String),
    Column("decimal_places", Integer),
    Column("is_system", Boolean),
    Column("is_active", Boolean),
)
item_types = tenant_table(
    "item_type",
    "item_type_id",
    Column("code", String),
    Column("name", Text),
    Column("is_stockable", Boolean),
    Column("is_system", Boolean),
)
groups = tenant_table(
    "item_group",
    "item_group_id",
    Column("code", String),
    Column("name", Text),
    Column("description", Text),
    Column("valuation_method", valuation_type),
    Column("default_item_type_id", UUID(as_uuid=True)),
    Column("is_active", Boolean),
)
group_attributes = tenant_table(
    "item_group_attribute",
    "attribute_id",
    Column("item_group_id", UUID(as_uuid=True)),
    Column("name", Text),
    Column("sort_order", Integer),
)
items = tenant_table(
    "item",
    "item_id",
    Column("sku", String),
    Column("name", Text),
    Column("description", Text),
    Column("item_kind", String),
    Column("item_group_id", UUID(as_uuid=True)),
    Column("item_type_id", UUID(as_uuid=True)),
    Column("abc_class", String),
    Column("barcode", String),
    Column("hsn_sac_id", UUID(as_uuid=True)),
    Column("gst_rate", Numeric(7, 4)),
    Column("is_tax_exempt", Boolean),
    Column("stock_uom_id", UUID(as_uuid=True)),
    Column("purchase_uom_id", UUID(as_uuid=True)),
    Column("sales_uom_id", UUID(as_uuid=True)),
    Column("valuation_method", valuation_type),
    Column("valuation_locked_at", DateTime(timezone=True)),
    Column("track_batch", Boolean),
    Column("track_lot", Boolean),
    Column("track_serial", Boolean),
    Column("track_expiry", Boolean),
    Column("shelf_life_days", Integer),
    Column("cost_center_id", UUID(as_uuid=True)),
    Column("cost_price", Numeric(20, 6)),
    Column("selling_price", Numeric(20, 6)),
    Column("safety_stock", Numeric(18, 4)),
    Column("is_returnable", Boolean),
    Column("status", String),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("catalog_version", Integer),
)
conversions = tenant_table(
    "uom_conversion",
    "uom_conversion_id",
    Column("item_id", UUID(as_uuid=True)),
    Column("from_uom_id", UUID(as_uuid=True)),
    Column("to_uom_id", UUID(as_uuid=True)),
    Column("factor", Numeric(24, 10)),
    Column("rounding_mode", String),
)
components = tenant_table(
    "composite_component",
    "composite_component_id",
    Column("kit_item_id", UUID(as_uuid=True)),
    Column("component_item_id", UUID(as_uuid=True)),
    Column("quantity", Numeric(18, 4)),
    Column("uom_id", UUID(as_uuid=True)),
    Column("scrap_pct", Numeric(7, 4)),
    Column("sort_order", Integer),
)
attribute_values = Table(
    "item_attribute_value",
    catalog_metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("item_id", UUID(as_uuid=True), primary_key=True),
    Column("attribute_id", UUID(as_uuid=True), primary_key=True),
    Column("value", String),
)
hsn_codes = tenant_table(
    "hsn_sac",
    "hsn_sac_id",
    Column("code", String),
    Column("description", Text),
    Column("default_gst_rate", Numeric(7, 4)),
)
cost_centers = tenant_table(
    "cost_center",
    "cost_center_id",
    Column("code", String),
    Column("name", Text),
    Column("is_active", Boolean),
)
tenants = Table(
    "tenant",
    catalog_metadata,
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("name", Text),
    Column("slug", String),
    Column("status", String),
    Column("default_valuation_method", valuation_type),
    Column("fiscal_year_start_month", Integer),
)
audit_log = Table(
    "audit_log",
    catalog_metadata,
    Column("tenant_id", UUID(as_uuid=True)),
    Column("actor_user_id", UUID(as_uuid=True)),
    Column("action", Text),
    Column("entity_table", Text),
    Column("entity_pk", Text),
    Column("new_value", JSONB),
    Column("request_id", Text),
)
ledger = Table(
    "stock_ledger_entry",
    catalog_metadata,
    Column("tenant_id", UUID(as_uuid=True)),
    Column("item_id", UUID(as_uuid=True)),
)
