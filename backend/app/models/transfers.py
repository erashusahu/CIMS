"""Existing transfer documents and the 3E operation history in cims."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

metadata = MetaData(schema="cims")

orders = Table(
    "transfer_order",
    metadata,
    Column("transfer_order_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True)),
    Column("transfer_no", Text),
    Column("status", Text),
    Column("from_location_id", UUID(as_uuid=True)),
    Column("to_location_id", UUID(as_uuid=True)),
    Column("in_transit_location_id", UUID(as_uuid=True)),
    Column("requested_by", UUID(as_uuid=True)),
    Column("dispatch_at", DateTime(timezone=True)),
    Column("dispatched_by", UUID(as_uuid=True)),
    Column("received_at", DateTime(timezone=True)),
    Column("received_by", UUID(as_uuid=True)),
    Column("expected_arrival_date", Date),
    Column("has_variance", Boolean),
    Column("notes", Text),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True)),
)
lines = Table(
    "transfer_order_line",
    metadata,
    Column("transfer_order_line_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True)),
    Column("transfer_order_id", UUID(as_uuid=True)),
    Column("line_no", Integer),
    Column("item_id", UUID(as_uuid=True)),
    Column("uom_id", UUID(as_uuid=True)),
    Column("requested_quantity", Numeric(18, 4)),
    Column("dispatched_quantity", Numeric(18, 4)),
    Column("received_quantity", Numeric(18, 4)),
    Column("short_quantity", Numeric(18, 4)),
    Column("damaged_quantity", Numeric(18, 4)),
    Column("quantity_in_stock_uom", Numeric(18, 4)),
)
operations = Table(
    "transfer_operation",
    metadata,
    Column("transfer_operation_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True)),
    Column("transfer_order_id", UUID(as_uuid=True)),
    Column("transfer_order_line_id", UUID(as_uuid=True)),
    Column("action", Text),
    Column("quantity", Numeric(18, 4)),
    Column("unit_cost", Numeric(20, 6)),
    Column("source_fifo_layer_id", BigInteger),
    Column("transit_fifo_layer_id", BigInteger),
    Column("destination_fifo_layer_id", BigInteger),
    Column("source_operation_id", UUID(as_uuid=True)),
    Column("ledger_entry_id", BigInteger),
    Column("ledger_entry_at", DateTime(timezone=True)),
    Column("reason", Text),
    Column("approved_by", UUID(as_uuid=True)),
    Column("created_by", UUID(as_uuid=True)),
    Column("created_at", DateTime(timezone=True)),
)

registrations = Table(
    "tax_registration",
    metadata,
    Column("tax_registration_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True)),
    Column("gstin", Text),
    Column("legal_name", Text),
    Column("state_code", Text),
    Column("registration_type", Text),
    Column("is_primary", Boolean),
)
