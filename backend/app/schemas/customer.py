"""Customer setup contracts for the first Phase 3C increment."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.catalog import Identifier, ImportIssue


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: Identifier
    name: str = Field(min_length=1, max_length=200)
    party_type: Literal["B2B", "B2C"] = "B2B"
    gstin: str | None = Field(
        default=None, pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
    )
    pan: str | None = Field(default=None, max_length=10)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    tax_treatment: Literal["REGISTERED", "UNREGISTERED", "COMPOSITION", "SEZ", "EXPORT"] = (
        "REGISTERED"
    )
    place_of_supply_state: str | None = Field(default=None, pattern=r"^[0-9]{2}$")
    payment_terms_days: int = Field(default=0, ge=0, le=3650)
    credit_limit: Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=4)] = Decimal(0)
    credit_hold: bool = False
    is_active: bool = True

    @field_validator("gstin", "pan", mode="before")
    @classmethod
    def uppercase(cls, value):
        return value.strip().upper() or None if isinstance(value, str) else value


class CustomerUpdate(CustomerIn):
    expected_updated_at: datetime


class CustomerOut(CustomerIn):
    model_config = ConfigDict(extra="ignore")
    customer_id: UUID
    created_at: datetime
    updated_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    page_size: int


class AddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address_type: Literal["BILLING", "SHIPPING", "WAREHOUSE", "OTHER"]
    label: str | None = Field(default=None, max_length=100)
    contact_name: str | None = Field(default=None, max_length=150)
    contact_phone: str | None = Field(default=None, max_length=40)
    line1: str = Field(min_length=1, max_length=250)
    line2: str | None = Field(default=None, max_length=250)
    city: str = Field(min_length=1, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    state_code: str = Field(pattern=r"^[0-9]{2}$")
    pincode: str | None = Field(default=None, max_length=10)
    country_code: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    gstin: str | None = Field(
        default=None, pattern=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
    )
    is_default: bool = False


class AddressOut(AddressIn):
    model_config = ConfigDict(extra="ignore")
    address_id: UUID
    customer_id: UUID


class CustomerCsvIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csv_text: str = Field(min_length=1, max_length=512_000)


class CustomerImportPreview(BaseModel):
    row_count: int
    valid_count: int
    can_import: bool
    issues: list[ImportIssue]
    rows: list[CustomerIn]
