"""Strict mobile-sync envelope. No credentials or arbitrary API paths are accepted."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MobileKind = Literal[
    "RECEIPT_DRAFT",
    "PICK_SCAN",
    "SHIPMENT_PACK",
    "TRANSFER_RECEIVE",
    "COUNT_SUBMIT",
    "POS_DRAFT",
]


class MobileOperationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    kind: MobileKind
    document_id: UUID | None = None
    expected_version: str | None = Field(default=None, max_length=100)
    depends_on: list[UUID] = Field(default_factory=list, max_length=20)
    captured_at: datetime
    payload: dict[str, Any]

    @field_validator("captured_at")
    @classmethod
    def timezone_required(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Include a timezone in the capture time")
        return value


class MobileReceiptOut(BaseModel):
    operation_id: UUID
    kind: MobileKind
    document_id: UUID | None
    captured_at: datetime
    accepted_at: datetime
    result: dict[str, Any]
