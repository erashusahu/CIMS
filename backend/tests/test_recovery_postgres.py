"""Stage 3G physical returns, count freezes and approved adjustments."""

import os
import uuid

import pytest
from sqlalchemy import select
from test_shipping_postgres import packed, picked_order, scan, ship

from app.api.v1.recovery import commit
from app.core.errors import ApiError
from app.models import inventory as stock
from app.schemas.recovery import CountIn, CountSubmit, ReturnIn
from app.services.counts import CountService
from app.services.returns import ReturnService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def run(state, service_type, method, *args):
    async with state["catalog"]() as catalog:
        service = service_type(catalog.session, catalog.context)
        return await commit(catalog.session, getattr(service, method)(*args))


async def balance(state, item_id, location_id):
    async with state["catalog"]() as catalog:
        return (
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


@pytest.mark.asyncio
async def test_return_restock_and_quarantine_do_not_double_post(catalog_db):
    item, location, order = await picked_order(catalog_db, quantity="2", stock="10")
    reservation = order["reservations"][0]
    await scan(catalog_db, order, reservation, item["sku"], "2")
    package = await packed(catalog_db, order, reservation, location, "2")
    shipment = await ship(catalog_db, package)
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 8
    good = await run(
        catalog_db,
        ReturnService,
        "create",
        ReturnIn(
            shipment_id=shipment["shipment_id"],
            reason_note="Customer returned item",
            lines=[{"item_id": item["item_id"], "quantity": "1", "condition": "GOOD"}],
        ),
    )
    await run(catalog_db, ReturnService, "approve", good["sales_return_id"], uuid.uuid4().hex)
    key = uuid.uuid4().hex
    received = await run(catalog_db, ReturnService, "receive", good["sales_return_id"], key)
    replay = await run(catalog_db, ReturnService, "receive", good["sales_return_id"], key)
    assert received["status"] == replay["status"] == "RECEIVED"
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 9
    damaged = await run(
        catalog_db,
        ReturnService,
        "create",
        ReturnIn(
            shipment_id=shipment["shipment_id"],
            reason_note="Packaging is damaged",
            lines=[{"item_id": item["item_id"], "quantity": "1", "condition": "DAMAGED"}],
        ),
    )
    await run(catalog_db, ReturnService, "approve", damaged["sales_return_id"], uuid.uuid4().hex)
    received_damaged = await run(
        catalog_db, ReturnService, "receive", damaged["sales_return_id"], uuid.uuid4().hex
    )
    assert (await balance(catalog_db, item["item_id"], received_damaged["quarantine_location_id"]))[
        "physical_on_hand"
    ] == 1
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 9
    async with catalog_db["catalog"]() as catalog:
        returned = (
            (
                await catalog.session.execute(
                    select(stock.ledger).where(
                        stock.ledger.c.tenant_id == catalog.context.tenant_id,
                        stock.ledger.c.source_document_type == "SALES_RETURN",
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(returned) == 2
        assert all(row["is_reversal"] and row["reversal_of_entry_id"] for row in returned)


@pytest.mark.asyncio
async def test_count_freeze_blocks_shipping_until_cancel(catalog_db):
    item, location, order = await picked_order(catalog_db, quantity="2", stock="10")
    reservation = order["reservations"][0]
    await scan(catalog_db, order, reservation, item["sku"], "2")
    package = await packed(catalog_db, order, reservation, location, "2")
    count = await run(
        catalog_db,
        CountService,
        "create",
        CountIn(
            location_id=location["location_id"],
            item_ids=[item["item_id"]],
            blind_count=True,
        ),
    )
    started = await run(catalog_db, CountService, "start", count["audit_count_id"])
    assert started["status"] == "IN_PROGRESS"
    assert started["lines"][0]["system_quantity"] is None
    with pytest.raises(ApiError) as frozen:
        await ship(catalog_db, package)
    assert frozen.value.code == "count_scope_frozen"
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 10
    await run(catalog_db, CountService, "cancel", count["audit_count_id"])
    shipped = await ship(catalog_db, package)
    assert shipped["status"] == "SHIPPED"
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 8


@pytest.mark.asyncio
async def test_count_submission_is_read_only_and_approved_post_is_idempotent(catalog_db):
    item, location, _ = await picked_order(catalog_db, quantity="1", stock="10")
    count = await run(
        catalog_db,
        CountService,
        "create",
        CountIn(
            location_id=location["location_id"],
            item_ids=[item["item_id"]],
            blind_count=True,
        ),
    )
    await run(catalog_db, CountService, "start", count["audit_count_id"])
    submitted = await run(
        catalog_db,
        CountService,
        "submit",
        count["audit_count_id"],
        CountSubmit(
            lines=[{"item_id": item["item_id"], "counted_quantity": "9"}],
        ),
    )
    assert submitted["status"] == "SUBMITTED"
    assert submitted["lines"][0]["system_quantity"] == 10
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 10
    approved = await run(catalog_db, CountService, "approve", count["audit_count_id"])
    assert approved["status"] == "APPROVED" and approved["lines"][0]["adjustment_id"]
    key = uuid.uuid4().hex
    posted = await run(catalog_db, CountService, "post", count["audit_count_id"], key)
    replay = await run(catalog_db, CountService, "post", count["audit_count_id"], key)
    assert posted["status"] == replay["status"] == "POSTED"
    assert (await balance(catalog_db, item["item_id"], location["location_id"]))[
        "physical_on_hand"
    ] == 9
