from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from app.schemas.catalog import ImportIssue, Money
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StockBalanceOut(BaseModel):
    item_id: UUID
    sku: str
    item_name: str
    location_id: UUID
    location_code: str
    location_name: str
    physical_on_hand: Decimal
    accounting_on_hand: Decimal
    committed: Decimal
    incoming: Decimal
    available: Decimal
    safety_stock: Decimal
    atp: Decimal
    eligible_for_sales: bool
    valuation_method: Literal["FIFO", "WEIGHTED_AVERAGE"]
    inventory_value: Decimal | None
    valuation_complete: bool
    last_movement_at: datetime | None


class MovementOut(BaseModel):
    entry_id: int
    occurred_at: datetime
    item_id: UUID
    sku: str
    location_id: UUID
    location_code: str
    stock_state: Literal["PHYSICAL", "ACCOUNTING", "COMMITTED", "INCOMING"]
    direction: Literal["IN", "OUT"]
    quantity: Decimal
    unit_cost: Decimal | None
    source_document_type: str | None
    source_document_id: UUID | None
    source_document_no: str | None
    physical_balance_after: Decimal | None


class StockDifferenceOut(BaseModel):
    item_id: UUID
    location_id: UUID
    physical_difference: Decimal
    accounting_difference: Decimal
    committed_difference: Decimal
    incoming_difference: Decimal


class StockPage[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=150)
    type: Literal["WAREHOUSE", "GODOWN", "STORE", "OFFICE"] = "WAREHOUSE"
    address: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=100)
    state_code: str | None = Field(default=None, max_length=3)
    pincode: str | None = Field(default=None, max_length=10)


class LocationOption(BaseModel):
    location_id: UUID
    code: str
    name: str
    is_active: bool


class OpeningLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: UUID
    location_id: UUID
    uom_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=10)]
    unit_cost: Money = Field(description="INR per stock unit, not per purchase carton")


class OpeningIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    business_date: date
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[OpeningLineIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_item_locations(self):
        pairs = {(line.item_id, line.location_id) for line in self.lines}
        if len(pairs) != len(self.lines):
            raise ValueError("Use one line per item and location in an opening-stock document")
        return self


class OpeningUpdate(OpeningIn):
    expected_version: int = Field(ge=1)


class DocumentVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class OpeningLineOut(BaseModel):
    opening_stock_line_id: UUID
    line_no: int
    item_id: UUID
    location_id: UUID
    entered_uom_id: UUID
    entered_quantity: Decimal
    conversion_factor: Decimal
    stock_uom_id: UUID
    stock_quantity: Decimal
    unit_cost: Decimal
    item_catalog_version: int
    sku_snapshot: str
    item_name_snapshot: str
    stock_unit_snapshot: str
    location_code_snapshot: str


class OpeningOut(BaseModel):
    opening_stock_id: UUID
    document_no: str
    business_date: date
    status: Literal["DRAFT", "POSTED", "CANCELLED"]
    version: int
    notes: str | None
    created_at: datetime
    posted_at: datetime | None
    lines: list[OpeningLineOut]


class OpeningCsvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_date: date
    csv_text: str = Field(min_length=1, max_length=512_000)


class OpeningImportPreview(BaseModel):
    row_count: int
    valid_count: int
    can_import: bool
    issues: list[ImportIssue]
    rows: list[dict[str, str]]
