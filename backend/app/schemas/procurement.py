"""Public supplier and preferred-supplier contracts for the first 3B increment."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.catalog import Identifier, ImportIssue, Money, Quantity


class SupplierIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Identifier
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    gstin: str | None = Field(
        default=None, pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
    )
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    tax_treatment: Literal["REGISTERED", "UNREGISTERED", "COMPOSITION", "SEZ", "EXPORT"] = (
        "REGISTERED"
    )
    payment_terms_days: int = Field(default=0, ge=0, le=3650)
    lead_time_days: int | None = Field(default=None, ge=0, le=3650)
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("gstin", mode="before")
    @classmethod
    def normalize_gstin(cls, value):
        return value.strip().upper() or None if isinstance(value, str) else value

    @field_validator("legal_name", "email", "phone", "notes", mode="before")
    @classmethod
    def empty_to_none(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class SupplierUpdate(SupplierIn):
    expected_updated_at: datetime


class SupplierOut(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    vendor_id: UUID
    code: str
    name: str
    legal_name: str | None
    gstin: str | None
    email: str | None
    phone: str | None
    tax_treatment: str
    payment_terms_days: int
    lead_time_days: int | None
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class SupplierPage(BaseModel):
    items: list[SupplierOut]
    total: int
    page: int
    page_size: int


class SupplierCsvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_text: str = Field(min_length=1, max_length=512_000)


class SupplierImportPreview(BaseModel):
    row_count: int
    valid_count: int
    can_import: bool
    issues: list[ImportIssue]
    rows: list[SupplierIn]


class PreferredSupplierIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item_id: UUID
    vendor_id: UUID
    vendor_item_code: str | None = Field(default=None, max_length=100)
    unit_price: Money | None = None
    min_order_qty: Quantity = Decimal(0)
    lead_time_days: int | None = Field(default=None, ge=0, le=3650)


class PreferredSupplierOut(PreferredSupplierIn):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    item_vendor_id: UUID
    item_sku: str
    vendor_code: str
    vendor_name: str


class PreferredSupplierPage(BaseModel):
    items: list[PreferredSupplierOut]
    total: int
    page: int
    page_size: int


class ReorderRuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    location_id: UUID
    method: Literal["REORDER_POINT", "MIN_MAX", "PAR_LEVEL"] = "REORDER_POINT"
    reorder_point: Quantity = Decimal(0)
    reorder_qty: Quantity = Decimal(0)
    max_level: Quantity | None = None
    par_level: Quantity | None = None
    preferred_vendor_id: UUID | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def valid_method(self):
        if self.method == "REORDER_POINT" and (self.reorder_point <= 0 or self.reorder_qty <= 0):
            raise ValueError("Reorder point and quantity must be positive")
        if self.method == "MIN_MAX" and (
            self.max_level is None or self.max_level <= self.reorder_point
        ):
            raise ValueError("Maximum level must exceed the reorder point")
        if self.method == "PAR_LEVEL" and (self.par_level is None or self.par_level <= 0):
            raise ValueError("PAR level must be positive")
        return self


class ReorderRuleOut(ReorderRuleIn):
    model_config = ConfigDict(extra="ignore")
    reorder_rule_id: UUID
    item_sku: str
    item_name: str
    location_code: str
    vendor_code: str | None


class ReorderRulePage(BaseModel):
    items: list[ReorderRuleOut]
    total: int
    page: int
    page_size: int


class PurchaseSuggestionOut(BaseModel):
    item_id: UUID
    item_sku: str
    location_id: UUID
    location_code: str
    vendor_id: UUID | None
    vendor_name: str | None
    projected_available: Decimal
    outstanding_order_qty: Decimal
    suggested_qty: Decimal
    method: str


class PurchaseSuggestionGroup(BaseModel):
    vendor_id: UUID | None
    vendor_name: str
    items: list[PurchaseSuggestionOut]


class PurchaseOrderLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    uom_id: UUID
    quantity: Quantity = Field(gt=0)
    unit_price: Money
    discount_amount: Money = Decimal(0)
    tax_rate: Decimal = Field(default=Decimal(0), ge=0, le=100, max_digits=7, decimal_places=4)


class PurchaseOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    vendor_id: UUID
    location_id: UUID
    order_date: date
    expected_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[PurchaseOrderLineIn] = Field(min_length=1, max_length=100)


class PurchaseOrderUpdate(PurchaseOrderIn):
    expected_updated_at: datetime


class PurchaseOrderLineOut(BaseModel):
    purchase_order_line_id: UUID
    line_no: int
    item_id: UUID
    uom_id: UUID
    quantity: Decimal
    quantity_in_stock_uom: Decimal
    unit_price: Decimal
    discount_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    line_total: Decimal
    received_quantity: Decimal
    cancelled_quantity: Decimal


class PurchaseOrderOut(BaseModel):
    purchase_order_id: UUID
    po_no: str
    vendor_id: UUID
    location_id: UUID
    status: str
    approval_state: str
    grand_total: Decimal
    notes: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None
    lines: list[PurchaseOrderLineOut]


class PurchaseOrderPage(BaseModel):
    items: list[PurchaseOrderOut]
    total: int
    page: int
    page_size: int


class PurchaseOrderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_updated_at: datetime


class PurchaseOrderAmendment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_updated_at: datetime
    line_id: UUID
    new_quantity: Quantity = Field(gt=0)
    reason: str = Field(min_length=3, max_length=1000)


class ReceiptLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purchase_order_line_id: UUID
    received_quantity: Quantity = Decimal(0)
    damaged_quantity: Quantity = Decimal(0)
    short_quantity: Quantity = Decimal(0)

    @model_validator(mode="after")
    def nonempty(self):
        if self.received_quantity + self.damaged_quantity + self.short_quantity <= 0:
            raise ValueError("Record received, damaged or short quantity")
        return self


class ReceiptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    purchase_order_id: UUID
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[ReceiptLineIn] = Field(min_length=1, max_length=100)


class ReceiptLineOut(BaseModel):
    purchase_receive_line_id: UUID
    purchase_order_line_id: UUID | None
    item_id: UUID
    uom_id: UUID
    received_quantity: Decimal
    damaged_quantity: Decimal
    short_quantity: Decimal
    quantity_in_stock_uom: Decimal
    ledger_entry_id: int | None


class ReceiptOut(BaseModel):
    purchase_receive_id: UUID
    receive_no: str
    purchase_order_id: UUID | None
    vendor_id: UUID
    location_id: UUID
    status: str
    total_short_qty: Decimal
    total_damaged_qty: Decimal
    created_at: datetime
    lines: list[ReceiptLineOut]


class ReceiptPage(BaseModel):
    items: list[ReceiptOut]
    total: int
    page: int
    page_size: int


class ReceiptPost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=128)
