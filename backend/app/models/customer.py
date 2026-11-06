"""Tenant-scoped customer and address mappings from the existing cims schema."""

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, Numeric, String, Table, Text
from sqlalchemy.dialects.postgresql import ENUM, UUID

metadata = MetaData(schema="cims")
party_type = ENUM("B2B", "B2C", name="party_type_t", schema="cims", create_type=False)

customers = Table(
    "customer",
    metadata,
    Column("customer_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("code", String),
    Column("name", Text),
    Column("party_type", party_type),
    Column("gstin", String),
    Column("pan", Text),
    Column("email", String),
    Column("phone", Text),
    Column("tax_treatment", Text),
    Column("place_of_supply_state", String),
    Column("payment_terms_days", Integer),
    Column("credit_limit", Numeric(20, 4)),
    Column("credit_hold", Boolean),
    Column("is_active", Boolean),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
)

addresses = Table(
    "party_address",
    metadata,
    Column("address_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), nullable=False),
    Column("customer_id", UUID(as_uuid=True)),
    Column("vendor_id", UUID(as_uuid=True)),
    Column("address_type", Text),
    Column("label", Text),
    Column("contact_name", Text),
    Column("contact_phone", Text),
    Column("line1", Text),
    Column("line2", Text),
    Column("city", Text),
    Column("district", Text),
    Column("state_code", String),
    Column("pincode", Text),
    Column("country_code", String),
    Column("gstin", String),
    Column("is_default", Boolean),
)
