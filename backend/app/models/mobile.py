"""Accepted offline-operation receipts; business documents stay in their own tables."""

from sqlalchemy import Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(schema="cims")
receipts = Table(
    "mobile_operation_receipt",
    metadata,
    Column("operation_id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", UUID(as_uuid=True), primary_key=True),
    Column("actor_key", Text),
    Column("operation_kind", Text),
    Column("document_id", UUID(as_uuid=True)),
    Column("request_hash", Text),
    Column("captured_at", DateTime(timezone=True)),
    Column("accepted_at", DateTime(timezone=True)),
    Column("result", JSONB),
)
