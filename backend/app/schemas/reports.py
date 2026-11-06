"""Read-only operational report totals for the current business."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class OperationsOverview(BaseModel):
    scope: Literal["WHOLE_BUSINESS"]
    as_of: datetime
    active_items: int
    stock_positions: int
    below_safety_positions: int
    open_purchase_orders: int
    confirmed_sales_orders: int
    active_counts: int
    pending_outbox_events: int


class AgingLayerOut(BaseModel):
    fifo_layer_id: int
    item_id: UUID
    sku: str
    location_id: UUID
    location_code: str
    received_at: datetime
    age_days: int
    remaining_quantity: Decimal
    unit_cost: Decimal
    remaining_value: Decimal


class AgingPage(BaseModel):
    items: list[AgingLayerOut]
    total: int
    page: int
    page_size: int


class SalesSummaryRow(BaseModel):
    status: str
    source: str
    order_count: int
    order_value: Decimal
    estimated_tax: Decimal


class PurchaseSummaryRow(BaseModel):
    status: str
    currency: str
    order_count: int
    order_value: Decimal
    estimated_tax: Decimal


class OrderTaxSupportRow(BaseModel):
    tax_rate: Decimal
    line_count: int
    taxable_value: Decimal
    estimated_tax: Decimal
