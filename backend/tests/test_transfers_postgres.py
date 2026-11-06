"""Phase 3E acceptance checks on the disposable PostgreSQL cluster only."""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.catalog_access import catalog_context
from app.api.v1.transfers import commit
from app.core.errors import ApiError
from app.db.base import get_session
from app.main import app
from app.models import catalog as catalog_model
from app.models import inventory as stock
from app.schemas.catalog import ItemIn
from app.schemas.inventory import LocationIn, OpeningIn
from app.schemas.transfers import (
    RegistrationIn,
    TransferIn,
    TransferLossAction,
    TransferQuantityAction,
)
from app.services.opening_stock import OpeningStockService
from app.services.transfers import TransferService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def setup(state, *, method=None):
    item, unit, _, _ = await state["make_item"](
        sku=f"MOVE-{uuid.uuid4().hex[:8]}", valuation_method=method
    )
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        source = await opening.create_location(
            LocationIn(code="SOURCE", name="Source", state_code="29")
        )
        destination = await opening.create_location(
            LocationIn(code="DEST", name="Destination", state_code="29")
        )
        await catalog.session.commit()
    async with state["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        registration = await commit(
            catalog.session,
            service.create_registration(
                RegistrationIn(
                    gstin="29ABCDE1234F1Z5",
                    legal_name="Test business",
                    state_code="29",
                )
            ),
        )
    async with state["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        await commit(
            catalog.session,
            service.assign_registration(source["location_id"], registration["tax_registration_id"]),
        )
    async with state["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        await commit(
            catalog.session,
            service.assign_registration(
                destination["location_id"], registration["tax_registration_id"]
            ),
        )
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        draft = await commit(
            catalog.session,
            opening.save(
                OpeningIn(
                    business_date=date.today(),
                    lines=[
                        {
                            "item_id": item["item_id"],
                            "location_id": source["location_id"],
                            "uom_id": unit,
                            "quantity": "100",
                            "unit_cost": "20",
                        }
                    ],
                )
            ),
        )
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        await commit(
            catalog.session,
            opening.post(draft["opening_stock_id"], draft["version"], uuid.uuid4().hex),
        )
    return item, source, destination


async def transfer(state, source, destination, item):
    async with state["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        return await commit(
            catalog.session,
            service.create(
                TransferIn(
                    from_location_id=source["location_id"],
                    to_location_id=destination["location_id"],
                    lines=[{"item_id": item["item_id"], "quantity": "40"}],
                )
            ),
        )


async def action(state, order_id, method, *args, **kwargs):
    async with state["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        return await commit(catalog.session, getattr(service, method)(order_id, *args, **kwargs))


async def balance(state, item_id, location_id):
    async with state["catalog"]() as catalog:
        row = (
            (
                await catalog.session.execute(
                    select(stock.balances).where(
                        stock.balances.c.tenant_id == catalog.context.tenant_id,
                        stock.balances.c.item_id == item_id,
                        stock.balances.c.location_id == location_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        return row or {
            "physical_on_hand": Decimal(0),
            "committed": Decimal(0),
            "incoming": Decimal(0),
            "inventory_value": Decimal(0),
        }


@pytest.mark.asyncio
async def test_40_dispatched_38_received_two_lost_with_replay(catalog_db):
    item, source, destination = await setup(catalog_db)
    draft = await transfer(catalog_db, source, destination, item)
    order_id = draft["transfer_order_id"]
    line_id = draft["lines"][0]["transfer_order_line_id"]
    confirmed = await action(catalog_db, order_id, "confirm", "confirm-" + uuid.uuid4().hex)
    assert confirmed["status"] == "CONFIRMED"
    assert (await balance(catalog_db, item["item_id"], source["location_id"]))["committed"] == 40
    dispatched = await action(catalog_db, order_id, "dispatch", "dispatch-" + uuid.uuid4().hex)
    assert dispatched["status"] == "IN_TRANSIT"
    assert (await balance(catalog_db, item["item_id"], source["location_id"]))[
        "physical_on_hand"
    ] == 60
    assert (await balance(catalog_db, item["item_id"], dispatched["in_transit_location_id"]))[
        "physical_on_hand"
    ] == 40
    assert (
        sum(
            [
                (await balance(catalog_db, item["item_id"], location))["inventory_value"]
                for location in (
                    source["location_id"],
                    dispatched["in_transit_location_id"],
                    destination["location_id"],
                )
            ]
        )
        == 2000
    )
    receive = TransferQuantityAction(
        line_id=line_id, quantity="38", idempotency_key="receive-" + uuid.uuid4().hex
    )
    received = await action(catalog_db, order_id, "resolve", receive)
    replay = await action(catalog_db, order_id, "resolve", receive)
    assert replay["status"] == received["status"] == "PARTIALLY_RECEIVED"
    assert (await balance(catalog_db, item["item_id"], destination["location_id"]))[
        "physical_on_hand"
    ] == 38
    assert (await balance(catalog_db, item["item_id"], dispatched["in_transit_location_id"]))[
        "physical_on_hand"
    ] == 2
    assert (
        sum(
            [
                (await balance(catalog_db, item["item_id"], location))["inventory_value"]
                for location in (
                    source["location_id"],
                    dispatched["in_transit_location_id"],
                    destination["location_id"],
                )
            ]
        )
        == 2000
    )
    loss = TransferLossAction(
        line_id=line_id,
        quantity="2",
        reason="Goods lost during transit",
        idempotency_key="loss-" + uuid.uuid4().hex,
    )
    settled = await action(catalog_db, order_id, "resolve", loss, loss=True)
    assert settled["status"] == "SETTLED"
    assert settled["lines"][0]["short_quantity"] == 2
    assert (await balance(catalog_db, item["item_id"], dispatched["in_transit_location_id"]))[
        "physical_on_hand"
    ] == 0
    assert (await balance(catalog_db, item["item_id"], destination["location_id"]))["incoming"] == 0


@pytest.mark.asyncio
async def test_transfer_http_contract(catalog_db):
    item, source, destination = await setup(catalog_db)
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
                status = await client.get("/api/v1/transfers/status")
                assert status.status_code == 200 and status.json()["ready"]
                registrations = await client.get("/api/v1/transfers/registrations")
                assert registrations.status_code == 200 and len(registrations.json()) == 1
                locations = await client.get("/api/v1/transfers/locations")
                assert locations.status_code == 200 and len(locations.json()) == 2
                created = await client.post(
                    "/api/v1/transfers",
                    json={
                        "from_location_id": str(source["location_id"]),
                        "to_location_id": str(destination["location_id"]),
                        "lines": [{"item_id": str(item["item_id"]), "quantity": "2"}],
                    },
                )
                assert created.status_code == 201, created.json()
                order = created.json()
                confirmed = await client.post(
                    f"/api/v1/transfers/{order['transfer_order_id']}/confirm",
                    json={"idempotency_key": uuid.uuid4().hex},
                )
                assert confirmed.status_code == 200 and confirmed.json()["status"] == "CONFIRMED"
                dispatched = await client.post(
                    f"/api/v1/transfers/{order['transfer_order_id']}/dispatch",
                    json={"idempotency_key": uuid.uuid4().hex},
                )
                assert dispatched.status_code == 200 and dispatched.json()["status"] == "IN_TRANSIT"
                transit = await client.get("/api/v1/transfers/in-transit")
                assert transit.status_code == 200
                assert transit.json()[0]["outstanding_quantity"] == "2.0000"
                received = await client.post(
                    f"/api/v1/transfers/{order['transfer_order_id']}/receive",
                    json={
                        "line_id": order["lines"][0]["transfer_order_line_id"],
                        "quantity": "2",
                        "idempotency_key": uuid.uuid4().hex,
                    },
                )
                assert received.status_code == 200 and received.json()["status"] == "RECEIVED"
                transit = await client.get("/api/v1/transfers/in-transit")
                assert transit.status_code == 200 and transit.json() == []
                listed = await client.get("/api/v1/transfers")
                assert listed.status_code == 200 and len(listed.json()) == 1
        finally:
            app.dependency_overrides.pop(catalog_context, None)
            app.dependency_overrides.pop(get_session, None)


@pytest.mark.asyncio
async def test_cross_state_transfer_blocked_and_confirm_rollback(catalog_db):
    item, source, destination = await setup(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        other = await opening.create_location(
            LocationIn(code="OTHER", name="Other state", state_code="27")
        )
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as blocked:
            await commit(
                catalog.session,
                service.create(
                    TransferIn(
                        from_location_id=source["location_id"],
                        to_location_id=other["location_id"],
                        lines=[{"item_id": item["item_id"], "quantity": "1"}],
                    )
                ),
            )
    assert blocked.value.code == "transfer_gst_registration"
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        same_state_other = await opening.create_location(
            LocationIn(code="OTHER29", name="Other registration", state_code="29")
        )
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        alternate = await commit(
            catalog.session,
            service.create_registration(
                RegistrationIn(
                    gstin="29ABCDE1234F1Z6",
                    legal_name="Other business",
                    state_code="29",
                )
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        await commit(
            catalog.session,
            service.assign_registration(
                same_state_other["location_id"], alternate["tax_registration_id"]
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as different_registration:
            await commit(
                catalog.session,
                service.create(
                    TransferIn(
                        from_location_id=source["location_id"],
                        to_location_id=same_state_other["location_id"],
                        lines=[{"item_id": item["item_id"], "quantity": "1"}],
                    )
                ),
            )
    assert different_registration.value.code == "transfer_gst_registration"
    async with catalog_db["catalog"]() as catalog:
        source_item = await catalog.row(
            catalog_model.items,
            "item_id",
            item["item_id"],
        )
        second = await commit(
            catalog.session,
            catalog.save_item(
                ItemIn(
                    sku=f"EMPTY-{uuid.uuid4().hex[:8]}",
                    name="Empty item",
                    stock_uom_id=source_item["stock_uom_id"],
                    item_type_id=source_item["item_type_id"],
                )
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = TransferService(catalog.session, catalog.context)
        draft = await commit(
            catalog.session,
            service.create(
                TransferIn(
                    from_location_id=source["location_id"],
                    to_location_id=destination["location_id"],
                    lines=[
                        {"item_id": item["item_id"], "quantity": "40"},
                        {"item_id": second["item_id"], "quantity": "1"},
                    ],
                )
            ),
        )
    with pytest.raises(ApiError) as insufficient:
        await action(
            catalog_db, draft["transfer_order_id"], "confirm", "confirm-" + uuid.uuid4().hex
        )
    assert insufficient.value.code == "insufficient_transfer_stock"
    assert (await balance(catalog_db, item["item_id"], source["location_id"]))["committed"] == 0
    assert (await balance(catalog_db, item["item_id"], destination["location_id"]))["incoming"] == 0
    async with catalog_db["catalog"]() as catalog:
        with pytest.raises(DBAPIError):
            await catalog.session.execute(
                text(
                    "UPDATE cims.transfer_order SET status='RECEIVED' WHERE transfer_order_id=:id"
                ),
                {"id": draft["transfer_order_id"]},
            )


@pytest.mark.asyncio
async def test_fifo_transfer_preserves_layer_cost_and_blocks_excess(catalog_db):
    item, source, destination = await setup(catalog_db, method="FIFO")
    draft = await transfer(catalog_db, source, destination, item)
    order_id = draft["transfer_order_id"]
    await action(catalog_db, order_id, "confirm", "confirm-" + uuid.uuid4().hex)
    dispatched = await action(catalog_db, order_id, "dispatch", "dispatch-" + uuid.uuid4().hex)
    line_id = draft["lines"][0]["transfer_order_line_id"]
    with pytest.raises(ApiError) as excess:
        await action(
            catalog_db,
            order_id,
            "resolve",
            TransferQuantityAction(
                line_id=line_id, quantity="41", idempotency_key="excess-" + uuid.uuid4().hex
            ),
        )
    assert excess.value.code == "transfer_exceeds_transit"
    received = await action(
        catalog_db,
        order_id,
        "resolve",
        TransferQuantityAction(
            line_id=line_id, quantity="40", idempotency_key="receive-" + uuid.uuid4().hex
        ),
    )
    assert received["status"] == "RECEIVED"
    assert received["operations"][0]["source_fifo_layer_id"] is not None
    assert received["operations"][0]["transit_fifo_layer_id"] is not None
    assert received["operations"][1]["destination_fifo_layer_id"] is not None
    async with catalog_db["catalog"]() as catalog:
        layers = (
            (
                await catalog.session.execute(
                    select(stock.fifo_layers).where(
                        stock.fifo_layers.c.tenant_id == catalog.context.tenant_id,
                        stock.fifo_layers.c.item_id == item["item_id"],
                    )
                )
            )
            .mappings()
            .all()
        )
        assert sum(layer["remaining_qty"] for layer in layers) == 100
        assert sum(layer["remaining_qty"] * layer["unit_cost"] for layer in layers) == 2000
    assert (await balance(catalog_db, item["item_id"], dispatched["in_transit_location_id"]))[
        "physical_on_hand"
    ] == 0
