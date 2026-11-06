"""Offline operation replay is atomic and scoped to the current tenant."""

import os
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from test_sales_postgres import confirm, create_order, setup

from app.api.catalog_access import CatalogContext
from app.core.errors import ApiError
from app.models import mobile as mobile_db
from app.models import sales as sales_db
from app.schemas.mobile import MobileOperationIn
from app.services.mobile import MobileService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


def operation(identifier, kind, payload, *, document_id=None, depends_on=None, version=None):
    return MobileOperationIn(
        operation_id=identifier,
        kind=kind,
        document_id=document_id,
        expected_version=version,
        depends_on=depends_on or [],
        captured_at=datetime.now(UTC),
        payload=payload,
    )


@pytest.mark.asyncio
async def test_pos_draft_replay_and_receipt_recovery(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    identifier = uuid.uuid4()
    payload = {
        "customer_id": customer["customer_id"],
        "location_id": location["location_id"],
        "order_date": date.today().isoformat(),
        "sales_source": "POS",
        "lines": [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "2"}],
    }
    captured = operation(identifier, "POS_DRAFT", payload)
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        first = await mobile.submit(captured)
        await catalog.session.commit()
        assert first["result"]["status"] == "DRAFT"
        assert first["result"]["sales_source"] == "POS"
        assert first["result"]["reservations"] == []
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        assert await mobile.receipt(identifier) == first
        assert await mobile.submit(captured) == first
        assert (
            await catalog.session.scalar(select(func.count()).select_from(mobile_db.receipts)) == 1
        )
        assert await catalog.session.scalar(select(func.count()).select_from(sales_db.orders)) == 1
        different = captured.model_copy(update={"payload": {**payload, "notes": "changed"}})
        with pytest.raises(ApiError) as conflict:
            await mobile.submit(different)
        assert conflict.value.code == "mobile_operation_conflict"
        another_actor = CatalogContext(
            catalog.context.tenant_id,
            uuid.uuid4(),
            "Owner",
            "other actor",
            local=False,
        )
        with pytest.raises(ApiError) as private:
            await MobileService(catalog.session, another_actor).receipt(identifier)
        assert private.value.status_code == 404


@pytest.mark.asyncio
async def test_dependency_and_tenant_scope(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    payload = {
        "customer_id": customer["customer_id"],
        "location_id": location["location_id"],
        "order_date": date.today().isoformat(),
        "sales_source": "POS",
        "lines": [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "1"}],
    }
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    second = operation(second_id, "POS_DRAFT", payload, depends_on=[first_id])
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as blocked:
            await mobile.submit(second)
        assert blocked.value.code == "mobile_predecessor_pending"
        await catalog.session.rollback()
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        await mobile.submit(operation(first_id, "POS_DRAFT", payload))
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        await mobile.submit(second)
        await catalog.session.commit()
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as other:
        mobile = MobileService(other.session, other.context)
        with pytest.raises(ApiError) as hidden:
            await mobile.receipt(first_id)
        assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_pick_rechecks_version_and_replays_once(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    order = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "2"}],
    )
    confirmed = await confirm(catalog_db, order)
    payload = {
        "reservation_id": confirmed["reservations"][0]["reservation_id"],
        "scanned_code": items[0]["sku"],
        "quantity": "1",
    }
    identifier = uuid.uuid4()
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        stale = operation(
            identifier,
            "PICK_SCAN",
            payload,
            document_id=order["sales_order_id"],
            version="0",
        )
        with pytest.raises(ApiError) as conflict:
            await mobile.submit(stale)
        assert conflict.value.code == "mobile_document_stale"
        await catalog.session.rollback()
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        excess = operation(
            uuid.uuid4(),
            "PICK_SCAN",
            {**payload, "quantity": "3"},
            document_id=order["sales_order_id"],
            version=str(confirmed["revision"]),
        )
        with pytest.raises(ApiError) as unavailable:
            await mobile.submit(excess)
        assert unavailable.value.code == "pick_exceeds_reservation"
        await catalog.session.rollback()
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        valid = operation(
            identifier,
            "PICK_SCAN",
            payload,
            document_id=order["sales_order_id"],
            version=str(confirmed["revision"]),
        )
        receipt = await mobile.submit(valid)
        await catalog.session.commit()
        assert receipt["result"]["quantity"] == "1.0000"
    async with catalog_db["catalog"]() as catalog:
        mobile = MobileService(catalog.session, catalog.context)
        assert await mobile.submit(valid) == receipt
        assert (
            await catalog.session.scalar(select(func.count()).select_from(sales_db.pick_scans)) == 1
        )
