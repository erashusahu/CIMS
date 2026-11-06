"""Decimal valuation rules for the inventory posting service's next increment."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.core.errors import ApiError


def weighted_average_receipt(
    old_quantity: Decimal, old_unit_cost: Decimal, received_quantity: Decimal, unit_cost: Decimal
) -> Decimal:
    values = (old_quantity, old_unit_cost, received_quantity, unit_cost)
    if (
        any(not value.is_finite() for value in values)
        or received_quantity <= 0
        or min(old_unit_cost, unit_cost) < 0
    ):
        raise ApiError(
            422, "invalid_valuation", "Provide a positive receipt quantity and nonnegative costs"
        )
    with localcontext() as context:
        context.prec = 80
        positive_old = max(old_quantity, Decimal(0))
        cost = (positive_old * old_unit_cost + received_quantity * unit_cost) / (
            positive_old + received_quantity
        )
        return cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FifoLayer:
    layer_id: int
    received_at: datetime
    remaining_quantity: Decimal
    unit_cost: Decimal


@dataclass(frozen=True)
class FifoAllocation:
    layer_id: int
    quantity: Decimal
    unit_cost: Decimal


def plan_fifo_issue(layers: list[FifoLayer], quantity: Decimal) -> list[FifoAllocation]:
    if not quantity.is_finite() or quantity <= 0:
        raise ApiError(422, "invalid_quantity", "Issue quantity must be positive")
    if any(
        not layer.remaining_quantity.is_finite()
        or not layer.unit_cost.is_finite()
        or layer.remaining_quantity < 0
        or layer.unit_cost < 0
        for layer in layers
    ):
        raise ApiError(409, "invalid_fifo_layers", "FIFO layer quantities and costs require review")
    result = []
    with localcontext() as context:
        context.prec = 80
        remaining = quantity
        for layer in sorted(layers, key=lambda value: (value.received_at, value.layer_id)):
            take = min(remaining, layer.remaining_quantity)
            if take:
                result.append(FifoAllocation(layer.layer_id, take, layer.unit_cost))
                remaining -= take
            if remaining == 0:
                return result
    raise ApiError(409, "insufficient_fifo_stock", "FIFO layers do not contain enough stock")
