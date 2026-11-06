"""Stage 3H POS drafts stay separate from final checkout."""

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from test_sales_postgres import setup

from app.api.v1.sales import commit_sales
from app.core.errors import ApiError
from app.models import sales as sales_db
from app.schemas.sales import SalesOrderIn, SalesOrderUpdate
from app.services.pos import PosService
from app.services.sales_orders import SalesOrderService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


@pytest.mark.asyncio
async def test_pos_cart_walk_in_and_checkout_boundary(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        pos = PosService(catalog.session, catalog.context)
        walk_in = await pos.walk_in_customer(location["location_id"])
        replay = await pos.walk_in_customer(location["location_id"])
        assert walk_in["customer_id"] == replay["customer_id"]
        assert walk_in["party_type"] == "B2C"
        await catalog.session.commit()

    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        payload = SalesOrderIn(
            customer_id=walk_in["customer_id"],
            location_id=location["location_id"],
            order_date=date.today(),
            sales_source="POS",
            lines=[{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "2"}],
        )
        draft = await commit_sales(catalog.session, sales.save(payload))
        assert draft["sales_source"] == "POS"
        assert draft["status"] == "DRAFT"
        assert not draft["reservations"]

    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        listed = await sales.orders(1, 25, "POS")
        assert listed["total"] == 1
        assert listed["items"][0]["sales_order_id"] == draft["sales_order_id"]
        with pytest.raises(ApiError) as blocked:
            await commit_sales(
                catalog.session,
                sales.confirm(draft["sales_order_id"], draft["revision"], uuid.uuid4().hex),
            )
        assert blocked.value.code == "pos_checkout_unavailable"

    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as changed:
            await commit_sales(
                catalog.session,
                sales.save(
                    SalesOrderUpdate(
                        **payload.model_dump(exclude={"sales_source"}),
                        sales_source="WHOLESALE",
                        expected_revision=draft["revision"],
                    ),
                    draft["sales_order_id"],
                ),
            )
        assert changed.value.code == "sales_source_immutable"

    async with catalog_db["catalog"]() as catalog:
        row = await catalog.session.scalar(
            select(sales_db.orders.c.sales_source).where(
                sales_db.orders.c.sales_order_id == draft["sales_order_id"]
            )
        )
        assert row == "POS"
        with pytest.raises(IntegrityError):
            await catalog.session.execute(
                update(sales_db.orders)
                .where(sales_db.orders.c.sales_order_id == draft["sales_order_id"])
                .values(status="CONFIRMED")
            )
        await catalog.session.rollback()

    async with catalog_db["catalog"]() as catalog:
        pos = PosService(catalog.session, catalog.context)
        cancelled = await pos.discard(draft["sales_order_id"], draft["revision"])
        await catalog.session.commit()
        assert cancelled["status"] == "CANCELLED"
