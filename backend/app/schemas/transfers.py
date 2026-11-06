from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransferLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    from_location_id: UUID
    to_location_id: UUID
    expected_arrival_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[TransferLineIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct(self):
        if self.from_location_id == self.to_location_id:
            raise ValueError("Choose different source and destination locations")
        if len({line.item_id for line in self.lines}) != len(self.lines):
            raise ValueError("Use one line per item")
        return self


class TransferAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100)


class TransferQuantityAction(TransferAction):
    line_id: UUID
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class TransferLossAction(TransferQuantityAction):
    reason: str = Field(min_length=10, max_length=1000)


class TransferLineOut(BaseModel):
    transfer_order_line_id: UUID
    item_id: UUID
    line_no: int
    requested_quantity: Decimal
    dispatched_quantity: Decimal
    received_quantity: Decimal
    short_quantity: Decimal
    damaged_quantity: Decimal
    quantity_in_stock_uom: Decimal


class TransferOperationOut(BaseModel):
    transfer_operation_id: UUID
    transfer_order_line_id: UUID
    action: str
    quantity: Decimal
    unit_cost: Decimal
    source_fifo_layer_id: int | None
    transit_fifo_layer_id: int | None
    destination_fifo_layer_id: int | None
    source_operation_id: UUID | None
    reason: str | None
    approved_by: UUID | None
    created_at: datetime


class TransferOut(BaseModel):
    transfer_order_id: UUID
    transfer_no: str
    status: str
    from_location_id: UUID
    to_location_id: UUID
    in_transit_location_id: UUID
    expected_arrival_date: date | None
    dispatch_at: datetime | None
    received_at: datetime | None
    notes: str | None
    lines: list[TransferLineOut]
    operations: list[TransferOperationOut]


class RegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    gstin: str = Field(pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
    legal_name: str = Field(min_length=1, max_length=200)
    state_code: str = Field(pattern=r"^[0-9]{2}$")

    @model_validator(mode="after")
    def state_matches_gstin(self):
        if self.gstin[:2] != self.state_code:
            raise ValueError("The GSTIN state code must match the registration state")
        return self


class RegistrationOut(BaseModel):
    tax_registration_id: UUID
    gstin: str
    legal_name: str
    state_code: str


class LocationRegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tax_registration_id: UUID


class TransferLocationOut(BaseModel):
    location_id: UUID
    code: str
    name: str
    state_code: str | None
    tax_registration_id: UUID | None
    is_active: bool


class InTransitOut(BaseModel):
    transfer_order_id: UUID
    transfer_no: str
    transfer_order_line_id: UUID
    item_id: UUID
    from_location_id: UUID
    to_location_id: UUID
    in_transit_location_id: UUID
    outstanding_quantity: Decimal
