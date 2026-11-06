"""Phase 3C order, availability and reservation API contracts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.catalog import Quantity


class SalesLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    uom_id: UUID
    quantity: Quantity = Field(gt=0)
    location_id: UUID | None = None
    discount_pct: Decimal = Field(default=Decimal(0), ge=0, le=100, max_digits=7, decimal_places=4)


class SalesOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: UUID
    sales_source: Literal["MANUAL_RETAIL", "WHOLESALE", "POS"] = "MANUAL_RETAIL"
    location_id: UUID
    order_date: date
    expected_ship_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[SalesLineIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_lines(self):
        keys = [
            (line.item_id, line.location_id or self.location_id, line.uom_id) for line in self.lines
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Combine duplicate item, location and unit lines")
        return self


class SalesOrderUpdate(SalesOrderIn):
    expected_revision: int = Field(ge=1)


class SalesAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class SalesCancel(SalesAction):
    reason: str = Field(min_length=3, max_length=1000)


class SalesLineOut(BaseModel):
    sales_order_line_id: UUID
    line_no: int
    item_id: UUID
    uom_id: UUID
    location_id: UUID | None
    quantity: Decimal
    quantity_in_stock_uom: Decimal
    unit_price: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    taxable_value: Decimal
    tax_amount: Decimal
    line_total: Decimal
    reserved_quantity: Decimal
    shipped_quantity: Decimal
    invoiced_quantity: Decimal
    cancelled_quantity: Decimal


class ReservationOut(BaseModel):
    reservation_id: UUID
    sales_order_line_id: UUID
    item_id: UUID
    location_id: UUID
    quantity: Decimal
    consumed_quantity: Decimal
    state: str


class SalesOrderOut(BaseModel):
    sales_order_id: UUID
    order_no: str
    status: str
    sales_source: Literal["MANUAL_RETAIL", "WHOLESALE", "POS"]
    revision: int
    customer_id: UUID
    location_id: UUID | None
    order_date: date
    expected_ship_date: date | None
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    grand_total: Decimal
    credit_check_passed: bool | None
    credit_limit_at_order: Decimal | None
    notes: str | None
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    lines: list[SalesLineOut]
    reservations: list[ReservationOut]


class SalesOrderPage(BaseModel):
    items: list[SalesOrderOut]
    total: int
    page: int
    page_size: int


class AvailabilityComponent(BaseModel):
    item_id: UUID
    sku: str
    required_per_unit: Decimal
    available: Decimal


class AvailabilityOut(BaseModel):
    item_id: UUID
    location_id: UUID
    available_stock_units: Decimal
    sellable: bool
    components: list[AvailabilityComponent]


class CreditExposureOut(BaseModel):
    customer_id: UUID
    credit_limit: Decimal
    committed_uninvoiced: Decimal
    remaining_credit: Decimal
    credit_hold: bool


class PickScanIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reservation_id: UUID
    scanned_code: str = Field(min_length=1, max_length=150)
    quantity: Quantity = Field(gt=0)
    override_reason: str | None = Field(default=None, min_length=8, max_length=1000)


class PickScanOut(BaseModel):
    pick_scan_id: UUID
    reservation_id: UUID
    sales_order_line_id: UUID
    item_id: UUID
    location_id: UUID
    scanned_code: str
    quantity: Decimal
    shipped_quantity: Decimal
    is_override: bool
    override_reason: str | None
    created_at: datetime


class PickEntryOut(BaseModel):
    reservation_id: UUID
    sales_order_line_id: UUID
    item_id: UUID
    sku: str
    barcode: str | None
    location_id: UUID
    location_code: str
    reserved_quantity: Decimal
    shipped_quantity: Decimal
    picked_unshipped: Decimal
    remaining_to_pick: Decimal


class PickListOut(BaseModel):
    sales_order_id: UUID
    order_no: str
    status: str
    entries: list[PickEntryOut]


class ShipmentLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sales_order_line_id: UUID
    quantity: Quantity = Field(gt=0)


class PackageContentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reservation_id: UUID
    quantity: Quantity = Field(gt=0)


class PackageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    weight_kg: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)
    length_cm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)
    width_cm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)
    height_cm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=3)
    notes: str | None = Field(default=None, max_length=1000)
    contents: list[PackageContentIn] = Field(min_length=1, max_length=100)


class ShipmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    from_location_id: UUID
    lines: list[ShipmentLineIn] = Field(min_length=1, max_length=100)
    packages: list[PackageIn] = Field(min_length=1, max_length=100)
    carrier: str | None = Field(default=None, max_length=200)
    tracking_no: str | None = Field(default=None, max_length=200)
    tracking_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def unique_lines(self):
        ids = [line.sales_order_line_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise ValueError("Use one shipment line per sales order line")
        return self


class ShipmentPost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)


class PackageContentOut(BaseModel):
    package_line_id: UUID
    sales_order_line_id: UUID
    item_id: UUID
    quantity: Decimal


class PackageOut(BaseModel):
    package_id: UUID
    package_no: str
    status: str
    weight_kg: Decimal | None
    length_cm: Decimal | None
    width_cm: Decimal | None
    height_cm: Decimal | None
    notes: str | None
    contents: list[PackageContentOut]


class ShipmentLineOut(BaseModel):
    shipment_line_id: UUID
    sales_order_line_id: UUID
    item_id: UUID
    quantity: Decimal
    quantity_in_stock_uom: Decimal


class ShipmentOut(BaseModel):
    shipment_id: UUID
    shipment_no: str
    status: str
    sales_order_id: UUID
    from_location_id: UUID
    is_partial: bool
    shipped_at: datetime | None
    carrier: str | None
    tracking_no: str | None
    tracking_url: str | None
    notes: str | None
    created_at: datetime
    lines: list[ShipmentLineOut]
    packages: list[PackageOut]
