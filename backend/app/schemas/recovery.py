from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    condition: Literal["GOOD", "DAMAGED"] = "GOOD"


class ReturnIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    shipment_id: UUID
    reason_note: str = Field(min_length=10, max_length=1000)
    lines: list[ReturnLineIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_lines(self):
        if len({line.item_id for line in self.lines}) != len(self.lines):
            raise ValueError("Use one return line per shipped item")
        return self


class ReturnAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100)


class ShippedLineOption(BaseModel):
    item_id: UUID
    quantity_in_stock_uom: Decimal


class ShippedOption(BaseModel):
    shipment_id: UUID
    shipment_no: str
    from_location_id: UUID
    lines: list[ShippedLineOption]


class ReturnLineOut(BaseModel):
    sales_return_line_id: UUID
    item_id: UUID
    quantity: Decimal
    quantity_in_stock_uom: Decimal
    condition: str
    restock: bool


class ReturnOut(BaseModel):
    sales_return_id: UUID
    return_no: str
    status: str
    customer_id: UUID
    shipment_id: UUID | None
    location_id: UUID
    restock_location_id: UUID | None
    quarantine_location_id: UUID | None
    reason_note: str | None
    total_quantity: Decimal
    received_at: datetime | None
    lines: list[ReturnLineOut]


class CountIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    location_id: UUID
    count_type: Literal["CYCLE", "FULL"] = "CYCLE"
    item_ids: list[UUID] = Field(default_factory=list, max_length=200)
    blind_count: bool = True
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def scope(self):
        if self.count_type == "CYCLE" and not self.item_ids:
            raise ValueError("Choose at least one item for a cycle count")
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Count each item once")
        return self


class CountLineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    counted_quantity: Decimal = Field(ge=0, max_digits=18, decimal_places=4)


class CountSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[CountLineEntry] = Field(min_length=1, max_length=5000)


class CountLineOut(BaseModel):
    audit_count_line_id: UUID
    item_id: UUID
    system_quantity: Decimal | None = None
    counted_quantity: Decimal | None
    recount_quantity: Decimal | None
    adjustment_id: UUID | None


class CountOut(BaseModel):
    audit_count_id: UUID
    count_no: str
    status: str
    location_id: UUID
    count_type: str
    blind_count: bool
    freeze_location: bool
    snapshot_at: datetime | None
    notes: str | None
    lines: list[CountLineOut]
