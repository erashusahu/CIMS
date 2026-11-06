from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.core.errors import ApiError
from app.services.valuation import FifoLayer, plan_fifo_issue, weighted_average_receipt


def test_ten_at_100_and_ten_at_120_average_110():
    assert weighted_average_receipt(
        Decimal(10), Decimal(100), Decimal(10), Decimal(120)
    ) == Decimal("110.000000")


def test_weighted_average_uses_six_decimal_half_up_rounding():
    assert weighted_average_receipt(Decimal(2), Decimal(0), Decimal(1), Decimal(1)) == Decimal(
        "0.333333"
    )


def test_receipt_into_empty_or_negative_position_uses_receipt_cost():
    for quantity in [Decimal(0), Decimal(-5)]:
        assert weighted_average_receipt(
            quantity, Decimal(20), Decimal(10), Decimal(100)
        ) == Decimal("100.000000")


def test_fifo_uses_oldest_layers_and_leaves_inputs_unchanged():
    now = datetime.now(UTC)
    newer = FifoLayer(2, now, Decimal(10), Decimal(120))
    older = FifoLayer(1, now - timedelta(days=1), Decimal(10), Decimal(100))
    allocations = plan_fifo_issue([newer, older], Decimal(12))
    assert [(entry.layer_id, entry.quantity) for entry in allocations] == [
        (1, Decimal(10)),
        (2, Decimal(2)),
    ]
    assert sum(entry.quantity * entry.unit_cost for entry in allocations) == Decimal(1240)
    assert older.remaining_quantity == newer.remaining_quantity == Decimal(10)


def test_fifo_ties_are_broken_by_layer_identifier():
    now = datetime.now(UTC)
    result = plan_fifo_issue(
        [FifoLayer(2, now, Decimal(2), Decimal(20)), FifoLayer(1, now, Decimal(2), Decimal(10))],
        Decimal(1),
    )
    assert result[0].layer_id == 1


def test_insufficient_fifo_is_rejected_without_partial_consumption():
    layer = FifoLayer(1, datetime.now(UTC), Decimal(1), Decimal(10))
    with pytest.raises(ApiError) as failure:
        plan_fifo_issue([layer], Decimal(2))
    assert failure.value.code == "insufficient_fifo_stock"
    assert layer.remaining_quantity == 1


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_fifo_rejects_invalid_requested_quantities(quantity):
    with pytest.raises(ApiError):
        plan_fifo_issue([], Decimal(quantity))
