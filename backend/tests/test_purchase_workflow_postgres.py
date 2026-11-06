"""3B acceptance checks on the guarded disposable PostgreSQL cluster."""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.v1.procurement import commit_procurement
from app.core.errors import ApiError
from app.models import inventory as inventory_db
from app.schemas.inventory import LocationIn
from app.schemas.procurement import (
    PurchaseOrderAmendment,
    PurchaseOrderIn,
    ReceiptIn,
    ReorderRuleIn,
    SupplierIn,
)
from app.services.inventory import InventoryService
from app.services.opening_stock import OpeningStockService
from app.services.outbox import OutboxWorker
from app.services.procurement import ProcurementService
from app.services.purchase_orders import PurchaseOrderService
from app.services.purchase_pdf import render_purchase_order
from app.services.reorder import ReorderService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def setup_purchase(state, *, price="10", valuation_method=None):
    item, pcs, _, _ = await state["make_item"](valuation_method=valuation_method)
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        location = await opening.create_location(LocationIn(code="MAIN", name="Main warehouse"))
        supplier = await ProcurementService(catalog.session, catalog.context).save_supplier(
            SupplierIn(code="SUP", name="Supplier", lead_time_days=5)
        )
        await catalog.session.commit()
    payload = PurchaseOrderIn(
        vendor_id=supplier["vendor_id"],
        location_id=location["location_id"],
        order_date=date.today(),
        lines=[{"item_id": item["item_id"], "uom_id": pcs, "quantity": "10", "unit_price": price}],
    )
    async with state["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        order = await commit_procurement(catalog.session, service.save_order(payload))
    for action in ("submit", "approve", "send"):
        if action == "approve" and order["status"] != "PENDING_APPROVAL":
            continue
        async with state["catalog"]() as catalog:
            service = PurchaseOrderService(catalog.session, catalog.context)
            order = await commit_procurement(
                catalog.session,
                service.action(order["purchase_order_id"], action, order["updated_at"]),
            )
    return item, location, supplier, order


@pytest.mark.asyncio
async def test_receive_eight_from_ten_is_physical_only_and_idempotent(catalog_db):
    item, location, _, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        receipt = await commit_procurement(
            catalog.session,
            service.save_receipt(
                ReceiptIn(
                    purchase_order_id=order["purchase_order_id"],
                    lines=[
                        {
                            "purchase_order_line_id": order["lines"][0]["purchase_order_line_id"],
                            "received_quantity": "8",
                            "short_quantity": "2",
                        }
                    ],
                )
            ),
        )
    key = uuid.uuid4().hex
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        posted = await service.post_receipt(receipt["purchase_receive_id"], key)
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        replay = await commit_procurement(
            catalog.session, service.post_receipt(receipt["purchase_receive_id"], key)
        )
        assert posted["status"] == replay["status"] == "POSTED"
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == item["item_id"],
                        inventory_db.balances.c.location_id == location["location_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == Decimal(8)
        assert balance["accounting_on_hand"] == Decimal(0)
        current = await service.order(order["purchase_order_id"])
        assert current["status"] == "PARTIALLY_RECEIVED"
        assert current["lines"][0]["quantity"] - current["lines"][0][
            "received_quantity"
        ] == Decimal(2)
        result = await OutboxWorker(catalog.session, catalog.context).run_once()
        assert result["status"] == "DONE"


@pytest.mark.asyncio
async def test_damaged_quantity_is_quarantined_and_excess_requires_amendment(catalog_db):
    item, location, _, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(
                catalog.session,
                service.save_receipt(
                    ReceiptIn(
                        purchase_order_id=order["purchase_order_id"],
                        lines=[
                            {
                                "purchase_order_line_id": order["lines"][0][
                                    "purchase_order_line_id"
                                ],
                                "received_quantity": "11",
                            }
                        ],
                    )
                ),
            )
        assert failure.value.code == "receipt_exceeds_order"
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        receipt = await commit_procurement(
            catalog.session,
            service.save_receipt(
                ReceiptIn(
                    purchase_order_id=order["purchase_order_id"],
                    lines=[
                        {
                            "purchase_order_line_id": order["lines"][0]["purchase_order_line_id"],
                            "received_quantity": "8",
                            "damaged_quantity": "2",
                        }
                    ],
                )
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        await commit_procurement(
            catalog.session, service.post_receipt(receipt["purchase_receive_id"], uuid.uuid4().hex)
        )
    async with catalog_db["catalog"]() as catalog:
        balances = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == item["item_id"]
                    )
                )
            )
            .mappings()
            .all()
        )
        assert sorted(row["physical_on_hand"] for row in balances) == [Decimal(2), Decimal(8)]
        assert all(row["accounting_on_hand"] == 0 for row in balances)
        quarantine = await catalog.session.scalar(
            select(inventory_db.locations.c.location_id).where(
                inventory_db.locations.c.code.like("QUAR-%")
            )
        )
        assert quarantine is not None and quarantine != location["location_id"]
        stock = await InventoryService(catalog.session, catalog.context).balances(
            1, 10, item_id=item["item_id"]
        )
        damaged = next(row for row in stock["items"] if row["location_id"] == quarantine)
        assert not damaged["eligible_for_sales"] and damaged["atp"] == 0


@pytest.mark.asyncio
async def test_reorder_suggestion_accounts_for_outstanding_order(catalog_db):
    item, location, supplier, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        service = ReorderService(catalog.session, catalog.context)
        rule = await commit_procurement(
            catalog.session,
            service.save_rule(
                ReorderRuleIn(
                    item_id=item["item_id"],
                    location_id=location["location_id"],
                    preferred_vendor_id=supplier["vendor_id"],
                    reorder_point="15",
                    reorder_qty="5",
                )
            ),
        )
        assert rule["item_sku"] == item["sku"]
    async with catalog_db["catalog"]() as catalog:
        service = ReorderService(catalog.session, catalog.context)
        groups = await service.suggestions()
        assert len(groups) == 1 and groups[0]["vendor_name"] == "Supplier"
        assert groups[0]["items"][0]["outstanding_order_qty"] == Decimal(10)


@pytest.mark.asyncio
async def test_approval_amendment_and_pdf(catalog_db):
    item, location, supplier, order = await setup_purchase(catalog_db, price="6000")
    assert order["approval_state"] == "APPROVED"
    assert order["approved_at"] is not None
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        amended = await commit_procurement(
            catalog.session,
            service.amend(
                order["purchase_order_id"],
                PurchaseOrderAmendment(
                    expected_updated_at=order["updated_at"],
                    line_id=order["lines"][0]["purchase_order_line_id"],
                    new_quantity="11",
                    reason="Supplier confirmed one extra unit",
                ),
            ),
        )
        assert amended["status"] == "PENDING_APPROVAL"
        assert amended["lines"][0]["quantity"] == Decimal(11)
    for action in ("approve", "send"):
        async with catalog_db["catalog"]() as catalog:
            service = PurchaseOrderService(catalog.session, catalog.context)
            amended = await commit_procurement(
                catalog.session,
                service.action(order["purchase_order_id"], action, amended["updated_at"]),
            )
    content = render_purchase_order(amended, supplier, location, {item["item_id"]: item})
    assert content.startswith(b"%PDF-") and len(content) > 1500


@pytest.mark.asyncio
async def test_posted_receipt_line_cannot_be_edited(catalog_db):
    _, _, _, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        receipt = await commit_procurement(
            catalog.session,
            service.save_receipt(
                ReceiptIn(
                    purchase_order_id=order["purchase_order_id"],
                    lines=[
                        {
                            "purchase_order_line_id": order["lines"][0]["purchase_order_line_id"],
                            "received_quantity": "1",
                        }
                    ],
                )
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        await commit_procurement(
            catalog.session, service.post_receipt(receipt["purchase_receive_id"], uuid.uuid4().hex)
        )
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with catalog_db["catalog"]() as catalog:
        with pytest.raises(DBAPIError):
            await catalog.session.execute(
                text(
                    "UPDATE cims.purchase_receive_line SET received_quantity=2 "
                    "WHERE purchase_receive_id=:receipt_id"
                ),
                {"receipt_id": receipt["purchase_receive_id"]},
            )


@pytest.mark.asyncio
async def test_office_cannot_be_used_for_receiving(catalog_db):
    item, _, supplier, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        office = await OpeningStockService(catalog.session, catalog.context).create_location(
            LocationIn(code="OFF", name="Office", type="OFFICE")
        )
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(
                catalog.session,
                service.save_order(
                    PurchaseOrderIn(
                        vendor_id=supplier["vendor_id"],
                        location_id=office["location_id"],
                        order_date=date.today(),
                        lines=[
                            {
                                "item_id": item["item_id"],
                                "uom_id": order["lines"][0]["uom_id"],
                                "quantity": "1",
                                "unit_price": "10",
                            }
                        ],
                    )
                ),
            )
        assert failure.value.code == "purchase_location_forbidden"


@pytest.mark.asyncio
async def test_fifo_receipt_creates_layer_for_physical_stock(catalog_db):
    item, _, _, order = await setup_purchase(catalog_db, valuation_method="FIFO")
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        receipt = await commit_procurement(
            catalog.session,
            service.save_receipt(
                ReceiptIn(
                    purchase_order_id=order["purchase_order_id"],
                    lines=[
                        {
                            "purchase_order_line_id": order["lines"][0]["purchase_order_line_id"],
                            "received_quantity": "3",
                        }
                    ],
                )
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = PurchaseOrderService(catalog.session, catalog.context)
        await commit_procurement(
            catalog.session, service.post_receipt(receipt["purchase_receive_id"], uuid.uuid4().hex)
        )
    async with catalog_db["catalog"]() as catalog:
        layers = (
            (
                await catalog.session.execute(
                    select(inventory_db.fifo_layers).where(
                        inventory_db.fifo_layers.c.item_id == item["item_id"]
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(layers) == 1 and layers[0]["remaining_qty"] == Decimal(3)
