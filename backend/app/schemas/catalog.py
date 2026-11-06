from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]
Quantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
Factor = Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=10)]
Identifier = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Valuation = Literal["WEIGHTED_AVERAGE", "FIFO"]


class CatalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnitIn(CatalogInput):
    code: Identifier
    name: Name
    uom_class: Literal["COUNT", "MASS", "VOLUME", "LENGTH", "AREA", "TIME"] = "COUNT"
    decimal_places: int = Field(default=0, ge=0, le=4)
    is_active: bool = True


class UnitOut(UnitIn):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    code: str
    name: str
    uom_id: UUID
    decimal_places: int = Field(ge=0, le=6)


class ItemTypeIn(CatalogInput):
    code: Identifier
    name: Name
    is_stockable: bool = True


class ItemTypeOut(ItemTypeIn):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    code: str
    name: str
    item_type_id: UUID


class GroupIn(CatalogInput):
    code: Identifier
    name: Name
    description: str | None = Field(default=None, max_length=2000)
    valuation_method: Valuation | None = None
    default_item_type_id: UUID | None = None
    attributes: list[Name] = Field(min_length=1, max_length=8)

    @field_validator("attributes")
    @classmethod
    def unique_attributes(cls, values: list[str]) -> list[str]:
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Variant attribute names must be distinct")
        return values


class GroupOut(CatalogInput):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    item_group_id: UUID
    code: str
    name: str
    description: str | None
    valuation_method: str | None
    default_item_type_id: UUID | None
    is_active: bool
    attributes: list[str]


class ConversionIn(CatalogInput):
    from_uom_id: UUID
    factor: Factor


class ComponentIn(CatalogInput):
    component_item_id: UUID
    quantity: PositiveQuantity
    uom_id: UUID


class ItemFields(CatalogInput):
    sku: Identifier
    name: Name
    description: str | None = Field(default=None, max_length=4000)
    item_kind: Literal["STANDALONE", "VARIANT", "COMPOSITE"] = "STANDALONE"
    item_group_id: UUID | None = None
    item_type_id: UUID
    abc_class: Literal["A", "B", "C"] | None = None
    barcode: Identifier | None = None
    hsn_sac_id: UUID | None = None
    gst_rate: Annotated[Decimal, Field(ge=0, le=100, max_digits=7, decimal_places=4)] = Decimal(0)
    is_tax_exempt: bool = False
    stock_uom_id: UUID
    purchase_uom_id: UUID | None = None
    sales_uom_id: UUID | None = None
    valuation_method: Valuation | None = None
    cost_price: Money = Decimal(0)
    selling_price: Money = Decimal(0)
    safety_stock: Quantity = Decimal(0)
    is_returnable: bool = True
    status: Literal["ACTIVE", "INACTIVE", "DRAFT"] = "ACTIVE"
    cost_center_id: UUID | None = None
    conversions: list[ConversionIn] = Field(default_factory=list, max_length=30)
    components: list[ComponentIn] = Field(default_factory=list, max_length=100)
    attributes: dict[str, str] = Field(default_factory=dict)


class ItemIn(ItemFields):
    @model_validator(mode="after")
    def valid_structure(self):
        if self.is_tax_exempt and self.gst_rate != 0:
            raise ValueError("Tax-exempt items must have a zero tax rate")
        if (self.item_kind == "VARIANT") != (self.item_group_id is not None):
            raise ValueError("Only variants require a variant group")
        if self.item_kind != "VARIANT" and self.attributes:
            raise ValueError("Only variants can have variant attributes")
        if self.item_kind != "COMPOSITE" and self.components:
            raise ValueError("Only virtual kits can contain components")
        if self.item_kind == "COMPOSITE" and not self.components:
            raise ValueError("A virtual kit requires at least one component")
        if len({entry.from_uom_id for entry in self.conversions}) != len(self.conversions):
            raise ValueError("Provide one conversion per source unit")
        if any(entry.from_uom_id == self.stock_uom_id for entry in self.conversions):
            raise ValueError("The stock unit does not require a conversion")
        if len({entry.component_item_id for entry in self.components}) != len(self.components):
            raise ValueError("A component can appear only once in a virtual kit")
        if len(self.attributes) > 8 or any(
            not key or not value or len(key) > 200 or len(value) > 200
            for key, value in self.attributes.items()
        ):
            raise ValueError("Provide nonempty variant attributes of at most 200 characters")
        return self


class ItemUpdate(ItemIn):
    expected_version: int = Field(ge=1)


class ItemOut(ItemFields):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)
    sku: str
    name: str
    description: str | None
    barcode: str | None
    item_id: UUID
    catalog_version: int
    created_at: datetime
    updated_at: datetime
    valuation_locked_at: datetime | None


class ItemSummaryOut(BaseModel):
    item_id: UUID
    sku: str
    name: str
    barcode: str | None
    item_kind: str
    stock_uom_id: UUID
    selling_price: Decimal
    status: str
    catalog_version: int


class WorkspaceIn(CatalogInput):
    name: Name
    slug: Annotated[str, Field(min_length=3, max_length=63, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class WorkspaceOut(BaseModel):
    tenant_id: UUID
    name: str
    slug: str


class HsnOption(BaseModel):
    hsn_sac_id: UUID
    code: str
    description: str | None
    default_gst_rate: Decimal


class CostCenterOption(BaseModel):
    cost_center_id: UUID
    code: str
    name: str
    is_active: bool


class CatalogOptions(BaseModel):
    can_write: bool
    local_mode: bool
    units: list[UnitOut]
    item_types: list[ItemTypeOut]
    groups: list[GroupOut]
    hsn_codes: list[HsnOption]
    cost_centers: list[CostCenterOption]
    default_valuation_method: Valuation


class ConvertIn(CatalogInput):
    item_id: UUID
    from_uom_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=10)]


class ConvertOut(BaseModel):
    item_id: UUID
    stock_uom_id: UUID
    stock_quantity: Decimal


class CsvIn(CatalogInput):
    csv_text: str = Field(min_length=1, max_length=512_000)


class ImportIssue(BaseModel):
    row: int
    field: str
    message: str


class ImportPreview(BaseModel):
    row_count: int
    valid_count: int
    issues: list[ImportIssue]
    can_import: bool
    rows: list[dict[str, str]]


class CatalogPage[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int
