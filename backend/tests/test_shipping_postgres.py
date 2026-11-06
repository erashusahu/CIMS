"""Phase 3D picking, packing and shipment posting on isolated PostgreSQL."""

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from test_sales_postgres import confirm, create_order, setup

from app.api.catalog_access import catalog_context
from app.api.v1.sales import commit_sales
from app.core.errors import ApiError
from app.db.base import get_session
from app.main import app
from app.models import inventory as inventory_db
from app.models import sales as sales_db
from app.schemas.catalog import ItemIn
from app.schemas.sales import PickScanIn, ShipmentIn
from app.services.sales_orders import SalesOrderService
from app.services.shipping import ShippingService
from app.services.shipping_pdf import packing_slip_pdf

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def picked_order(state, *, quantity="3", stock="10", valuation_method=None):
    items, pcs, _, location, customer = await setup(
        state, stock=stock, valuation_method=valuation_method
    )
    draft = await create_order(
        state,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": quantity}],
    )
    confirmed = await confirm(state, draft)
    return items[0], location, confirmed


async def scan(state, order, reservation, code, quantity):
    async with state["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        return await commit_sales(
            catalog.session,
            shipping.scan(
                order["sales_order_id"],
                PickScanIn(
                    reservation_id=reservation["reservation_id"],
                    scanned_code=code,
                    quantity=quantity,
                ),
            ),
        )


async def packed(state, order, reservation, location, quantity="2"):
    line = order["lines"][0]
    async with state["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        return await commit_sales(
            catalog.session,
            shipping.draft(
                order["sales_order_id"],
                ShipmentIn(
                    from_location_id=location["location_id"],
                    lines=[
                        {"sales_order_line_id": line["sales_order_line_id"], "quantity": quantity}
                    ],
                    packages=[
                        {
                            "weight_kg": "0.5",
                            "length_cm": "20",
                            "width_cm": "10",
                            "height_cm": "5",
                            "contents": [
                                {
                                    "reservation_id": reservation["reservation_id"],
                                    "quantity": quantity,
                                },
                            ],
                        }
                    ],
                    carrier="Manual",
                    tracking_no="TRACK-1",
                ),
            ),
        )


async def ship(state, shipment, key=None):
    async with state["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        return await commit_sales(
            catalog.session, shipping.post(shipment["shipment_id"], key or uuid.uuid4().hex)
        )


@pytest.mark.asyncio
async def test_partial_shipment_changes_physical_and_commitment_once(catalog_db):
    item, location, order = await picked_order(catalog_db)
    reservation = order["reservations"][0]
    with pytest.raises(ApiError) as wrong:
        await scan(catalog_db, order, reservation, "WRONG", "1")
    assert wrong.value.code == "wrong_item_scan"
    with pytest.raises(ApiError) as excess:
        await scan(catalog_db, order, reservation, item["sku"], "4")
    assert excess.value.code == "pick_exceeds_reservation"
    await scan(catalog_db, order, reservation, item["sku"], "3")
    shipment = await packed(catalog_db, order, reservation, location)
    assert shipment["status"] == "PACKED"
    async with catalog_db["catalog"]() as catalog:
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == item["item_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == 10 and balance["committed"] == 3
        pdf = await packing_slip_pdf(
            ShippingService(catalog.session, catalog.context), shipment, order
        )
        assert pdf.startswith(b"%PDF")
    key = uuid.uuid4().hex
    result = await ship(catalog_db, shipment, key)
    assert result["status"] == "SHIPPED" and result["is_partial"]
    assert (await ship(catalog_db, shipment, key))["status"] == "SHIPPED"
    async with catalog_db["catalog"]() as catalog:
        current = await SalesOrderService(catalog.session, catalog.context).order(
            order["sales_order_id"]
        )
        assert current["status"] == "PARTIALLY_FULFILLED"
        assert current["lines"][0]["shipped_quantity"] == 2
        assert current["reservations"][0]["consumed_quantity"] == 2
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == item["item_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == 8 and balance["committed"] == 1
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        cancelled = await commit_sales(
            catalog.session,
            sales.cancel(
                order["sales_order_id"],
                current["revision"],
                uuid.uuid4().hex,
                "Cancel remaining item",
            ),
        )
        assert cancelled["lines"][0]["shipped_quantity"] == 2
        assert cancelled["lines"][0]["cancelled_quantity"] == 1
    async with catalog_db["catalog"]() as catalog:
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == item["item_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == 8 and balance["committed"] == 0


@pytest.mark.asyncio
async def test_packing_requires_scans_and_exact_contents(catalog_db):
    item, location, order = await picked_order(catalog_db)
    reservation = order["reservations"][0]
    with pytest.raises(ApiError) as missing:
        await packed(catalog_db, order, reservation, location)
    assert missing.value.code == "shipment_not_picked"
    await scan(catalog_db, order, reservation, item["sku"], "2")
    with pytest.raises(ApiError) as short:
        await packed(catalog_db, order, reservation, location, quantity="3")
    assert short.value.code == "shipment_not_picked"


@pytest.mark.asyncio
async def test_override_requires_reason_and_records_evidence(catalog_db):
    _, _, order = await picked_order(catalog_db)
    reservation = order["reservations"][0]
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as wrong:
            await commit_sales(
                catalog.session,
                shipping.scan(
                    order["sales_order_id"],
                    PickScanIn(
                        reservation_id=reservation["reservation_id"],
                        scanned_code="WRONG",
                        quantity="1",
                    ),
                ),
            )
        assert wrong.value.code == "wrong_item_scan"
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        override = await commit_sales(
            catalog.session,
            shipping.scan(
                order["sales_order_id"],
                PickScanIn(
                    reservation_id=reservation["reservation_id"],
                    scanned_code="WRONG",
                    quantity="1",
                    override_reason="Manager verified item label",
                ),
            ),
        )
        assert override["is_override"] and override["override_reason"]


@pytest.mark.asyncio
async def test_failed_second_shipment_line_rolls_back_first(catalog_db, monkeypatch):
    items, pcs, _, location, customer = await setup(catalog_db, item_count=2)
    draft = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": item["item_id"], "uom_id": pcs, "quantity": "1"} for item in items],
    )
    order = await confirm(catalog_db, draft)
    for reservation in order["reservations"]:
        item = next(value for value in items if value["item_id"] == reservation["item_id"])
        await scan(catalog_db, order, reservation, item["sku"], "1")
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        shipment = await commit_sales(
            catalog.session,
            shipping.draft(
                order["sales_order_id"],
                ShipmentIn(
                    from_location_id=location["location_id"],
                    lines=[
                        {"sales_order_line_id": line["sales_order_line_id"], "quantity": "1"}
                        for line in order["lines"]
                    ],
                    packages=[
                        {
                            "contents": [
                                {"reservation_id": reservation["reservation_id"], "quantity": "1"}
                                for reservation in order["reservations"]
                            ]
                        }
                    ],
                ),
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        original = shipping.posting.append_shipment_issue
        calls = 0

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ApiError(409, "simulated_issue_failure", "Second line failed")
            return await original(*args, **kwargs)

        monkeypatch.setattr(shipping.posting, "append_shipment_issue", fail_second)
        with pytest.raises(ApiError) as failure:
            await commit_sales(
                catalog.session, shipping.post(shipment["shipment_id"], uuid.uuid4().hex)
            )
        assert failure.value.code == "simulated_issue_failure"
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        assert (await shipping.shipment(shipment["shipment_id"]))["status"] == "PACKED"
        assert (await shipping.pick_list(order["sales_order_id"]))["status"] == "CONFIRMED"
        balances = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.tenant_id == catalog.tenant_id
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(balances) == 2
        assert all(row["physical_on_hand"] == 10 and row["committed"] == 1 for row in balances)


@pytest.mark.asyncio
async def test_fifo_shipment_consumes_layers(catalog_db):
    item, location, order = await picked_order(catalog_db, valuation_method="FIFO")
    reservation = order["reservations"][0]
    await scan(catalog_db, order, reservation, item["sku"], "2")
    shipment = await packed(catalog_db, order, reservation, location)
    await ship(catalog_db, shipment)
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
        assert sum(row["remaining_qty"] for row in layers) == 8
        issues = (
            (
                await catalog.session.execute(
                    select(inventory_db.fifo_consumptions).where(
                        inventory_db.fifo_consumptions.c.tenant_id == catalog.tenant_id
                    )
                )
            )
            .mappings()
            .all()
        )
        assert sum(row["quantity"] for row in issues) == 2


@pytest.mark.asyncio
async def test_kit_shipment_uses_component_reservation(catalog_db):
    items, pcs, kind, location, customer = await setup(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        kit = await catalog.save_item(
            ItemIn(
                sku="SHIP-KIT",
                name="Two items",
                item_kind="COMPOSITE",
                stock_uom_id=pcs,
                item_type_id=kind,
                selling_price="80",
                components=[
                    {"component_item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "2"}
                ],
            )
        )
        await catalog.session.commit()
    draft = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": kit["item_id"], "uom_id": pcs, "quantity": "3"}],
    )
    order = await confirm(catalog_db, draft)
    reservation = order["reservations"][0]
    await scan(catalog_db, order, reservation, items[0]["sku"], "4")
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as short:
            await commit_sales(
                catalog.session,
                shipping.draft(
                    order["sales_order_id"],
                    ShipmentIn(
                        from_location_id=location["location_id"],
                        lines=[
                            {
                                "sales_order_line_id": order["lines"][0]["sales_order_line_id"],
                                "quantity": "2",
                            }
                        ],
                        packages=[
                            {
                                "contents": [
                                    {
                                        "reservation_id": reservation["reservation_id"],
                                        "quantity": "2",
                                    }
                                ]
                            }
                        ],
                    ),
                ),
            )
        assert short.value.code == "package_quantity_mismatch"
    async with catalog_db["catalog"]() as catalog:
        shipping = ShippingService(catalog.session, catalog.context)
        shipment = await commit_sales(
            catalog.session,
            shipping.draft(
                order["sales_order_id"],
                ShipmentIn(
                    from_location_id=location["location_id"],
                    lines=[
                        {
                            "sales_order_line_id": order["lines"][0]["sales_order_line_id"],
                            "quantity": "2",
                        }
                    ],
                    packages=[
                        {
                            "contents": [
                                {"reservation_id": reservation["reservation_id"], "quantity": "4"}
                            ]
                        }
                    ],
                ),
            ),
        )
    await ship(catalog_db, shipment)
    async with catalog_db["catalog"]() as catalog:
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == items[0]["item_id"]
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == 6 and balance["committed"] == 2


@pytest.mark.asyncio
async def test_shipping_http_contract_and_packing_slip(catalog_db):
    item, location, order = await picked_order(catalog_db)
    reservation = order["reservations"][0]
    line = order["lines"][0]
    async with catalog_db["catalog"]() as catalog:

        async def context_override():
            return catalog.context

        async def session_override():
            yield catalog.session

        app.dependency_overrides[catalog_context] = context_override
        app.dependency_overrides[get_session] = session_override
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                pick = await client.get(f"/api/v1/sales/orders/{order['sales_order_id']}/pick-list")
                assert pick.status_code == 200, pick.json()
                assert pick.json()["entries"][0]["remaining_to_pick"] == "3.0000"
                scanned = await client.post(
                    f"/api/v1/sales/orders/{order['sales_order_id']}/scans",
                    json={
                        "reservation_id": str(reservation["reservation_id"]),
                        "scanned_code": item["sku"],
                        "quantity": "2",
                    },
                )
                assert scanned.status_code == 201, scanned.json()
                packed_response = await client.post(
                    f"/api/v1/sales/orders/{order['sales_order_id']}/shipments",
                    json={
                        "from_location_id": str(location["location_id"]),
                        "lines": [
                            {
                                "sales_order_line_id": str(line["sales_order_line_id"]),
                                "quantity": "2",
                            }
                        ],
                        "packages": [
                            {
                                "weight_kg": "0.5",
                                "contents": [
                                    {
                                        "reservation_id": str(reservation["reservation_id"]),
                                        "quantity": "2",
                                    }
                                ],
                            }
                        ],
                    },
                )
                assert packed_response.status_code == 201, packed_response.json()
                shipment = packed_response.json()
                slip = await client.get(
                    f"/api/v1/sales/shipments/{shipment['shipment_id']}/packing-slip"
                )
                assert slip.status_code == 200 and slip.content.startswith(b"%PDF")
                posted = await client.post(
                    f"/api/v1/sales/shipments/{shipment['shipment_id']}/post",
                    json={"idempotency_key": uuid.uuid4().hex},
                )
                assert posted.status_code == 200, posted.json()
                assert posted.json()["status"] == "SHIPPED"
        finally:
            app.dependency_overrides.pop(catalog_context, None)
            app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_posted_package_and_scan_evidence_are_immutable(catalog_db):
    item, location, order = await picked_order(catalog_db)
    reservation = order["reservations"][0]
    picked = await scan(catalog_db, order, reservation, item["sku"], "2")
    shipment = await packed(catalog_db, order, reservation, location)
    await ship(catalog_db, shipment)
    async with catalog_db["catalog"]() as catalog:
        with pytest.raises(DBAPIError):
            await catalog.session.execute(
                update(sales_db.package_lines)
                .where(sales_db.package_lines.c.package_id == shipment["packages"][0]["package_id"])
                .values(quantity=1)
            )
        await catalog.session.rollback()
    async with catalog_db["catalog"]() as catalog:
        with pytest.raises(DBAPIError):
            await catalog.session.execute(
                update(sales_db.pick_scans)
                .where(sales_db.pick_scans.c.pick_scan_id == picked["pick_scan_id"])
                .values(scanned_code="ALTERED")
            )
        await catalog.session.rollback()
