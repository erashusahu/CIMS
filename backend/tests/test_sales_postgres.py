"""Phase 3C behavior against the disposable PostgreSQL clone."""

import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.catalog_access import catalog_context
from app.api.v1.sales import commit_sales
from app.core.errors import ApiError
from app.db.base import get_session
from app.main import app
from app.models import inventory as inventory_db
from app.models import sales as sales_db
from app.schemas.catalog import ItemIn, ItemUpdate
from app.schemas.customer import CustomerIn
from app.schemas.inventory import LocationIn, OpeningIn
from app.schemas.sales import SalesOrderIn
from app.services.customer import CustomerService
from app.services.opening_stock import InventoryPostingService, OpeningStockService
from app.services.sales_orders import SalesOrderService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def setup(state, *, item_count=1, stock="10", credit="10000", valuation_method=None):
    item, pcs, _, kind = await state["make_item"](
        sku=f"SALE-0-{uuid.uuid4().hex[:6]}", valuation_method=valuation_method
    )
    items = [item]
    if item_count > 1:
        async with state["catalog"]() as catalog:
            for number in range(1, item_count):
                items.append(
                    await catalog.save_item(
                        ItemIn(
                            sku=f"SALE-{number}-{uuid.uuid4().hex[:6]}",
                            name="Sale item",
                            stock_uom_id=pcs,
                            item_type_id=kind,
                            selling_price="25",
                        )
                    )
                )
            await catalog.session.commit()
    async with state["catalog"]() as catalog:
        location = await OpeningStockService(catalog.session, catalog.context).create_location(
            LocationIn(code="MAIN", name="Main warehouse")
        )
        customer = await CustomerService(catalog.session, catalog.context).save(
            CustomerIn(code="BUYER", name="Buyer", credit_limit=credit)
        )
        await catalog.session.commit()
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        draft = await opening.save(
            OpeningIn(
                business_date=date.today(),
                lines=[
                    {
                        "item_id": item["item_id"],
                        "location_id": location["location_id"],
                        "uom_id": pcs,
                        "quantity": stock,
                        "unit_cost": "20",
                    }
                    for item in items
                ],
            )
        )
        await catalog.session.commit()
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        await opening.post(draft["opening_stock_id"], draft["version"], uuid.uuid4().hex)
        await catalog.session.commit()
    return items, pcs, kind, location, customer


async def create_order(state, customer, location, lines):
    async with state["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        return await commit_sales(
            catalog.session,
            sales.save(
                SalesOrderIn(
                    customer_id=customer["customer_id"],
                    location_id=location["location_id"],
                    order_date=date.today(),
                    lines=lines,
                )
            ),
        )


async def confirm(state, order, key=None):
    async with state["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        return await commit_sales(
            catalog.session,
            sales.confirm(
                order["sales_order_id"],
                order["revision"],
                key or uuid.uuid4().hex,
            ),
        )


@pytest.mark.asyncio
async def test_reserve_three_from_ten_then_cancel(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    order = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "3"}],
    )
    assert order["status"] == "DRAFT"
    assert order["lines"][0]["line_total"] == Decimal(75)
    key = uuid.uuid4().hex
    confirmed = await confirm(catalog_db, order, key)
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["reservations"][0]["quantity"] == 3
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        replay = await commit_sales(catalog.session, sales.confirm(order["sales_order_id"], 1, key))
        assert replay["status"] == "CONFIRMED"
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        available = await sales.availability(items[0]["item_id"], location["location_id"])
        credit = await sales.credit_exposure(customer["customer_id"])
        balance = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.item_id == items[0]["item_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == 10
        assert balance["committed"] == 3
        assert available["available_stock_units"] == 7
        assert credit["committed_uninvoiced"] == 75
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        cancelled = await commit_sales(
            catalog.session,
            sales.cancel(order["sales_order_id"], 2, uuid.uuid4().hex, "Customer withdrew order"),
        )
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["lines"][0]["cancelled_quantity"] == 3
        assert cancelled["reservations"][0]["state"] == "RELEASED"
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        assert (await sales.availability(items[0]["item_id"], location["location_id"]))[
            "available_stock_units"
        ] == 10
        assert (await sales.credit_exposure(customer["customer_id"]))["committed_uninvoiced"] == 0


@pytest.mark.asyncio
async def test_cancel_releases_only_remaining_unshipped_stock(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    draft = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "3"}],
    )
    confirmed = await confirm(catalog_db, draft)
    line = confirmed["lines"][0]
    reservation = confirmed["reservations"][0]
    async with catalog_db["catalog"]() as catalog:
        # Simulate Stage 3D consuming one reserved unit, without changing physical stock.
        now = await catalog.session.scalar(select(func.clock_timestamp()))
        await InventoryPostingService(catalog.session, catalog.context).append_reservation(
            confirmed,
            line["sales_order_line_id"],
            items[0]["item_id"],
            location["location_id"],
            Decimal(1),
            "OUT",
            now,
            uuid.uuid4().hex,
        )
        await catalog.session.execute(
            update(sales_db.reservations)
            .where(sales_db.reservations.c.reservation_id == reservation["reservation_id"])
            .values(consumed_quantity=1)
        )
        await catalog.session.execute(
            update(sales_db.lines)
            .where(sales_db.lines.c.sales_order_line_id == line["sales_order_line_id"])
            .values(shipped_quantity=1, reserved_quantity=2)
        )
        await catalog.session.execute(
            update(sales_db.orders)
            .where(sales_db.orders.c.sales_order_id == draft["sales_order_id"])
            .values(status="PARTIALLY_FULFILLED", revision=3)
        )
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        cancelled = await commit_sales(
            catalog.session,
            SalesOrderService(catalog.session, catalog.context).cancel(
                draft["sales_order_id"], 3, uuid.uuid4().hex, "Cancel remaining stock"
            ),
        )
        assert cancelled["lines"][0]["shipped_quantity"] == 1
        assert cancelled["lines"][0]["cancelled_quantity"] == 2
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
        assert balance["committed"] == 0
        assert (
            await SalesOrderService(catalog.session, catalog.context).credit_exposure(
                customer["customer_id"]
            )
        )["committed_uninvoiced"] == 25


@pytest.mark.asyncio
async def test_late_line_failure_rolls_back_every_reservation(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db, item_count=2)
    order = await create_order(
        catalog_db,
        customer,
        location,
        [
            {"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "3"},
            {"item_id": items[1]["item_id"], "uom_id": pcs, "quantity": "11"},
        ],
    )
    with pytest.raises(ApiError) as error:
        await confirm(catalog_db, order)
    assert error.value.code == "insufficient_stock"
    async with catalog_db["catalog"]() as catalog:
        sales = SalesOrderService(catalog.session, catalog.context)
        current = await sales.order(order["sales_order_id"])
        assert current["status"] == "DRAFT" and current["reservations"] == []
        balances = (
            (
                await catalog.session.execute(
                    select(inventory_db.balances).where(
                        inventory_db.balances.c.tenant_id == catalog.tenant_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        assert all(row["committed"] == 0 for row in balances)


@pytest.mark.asyncio
async def test_concurrent_last_unit_and_credit_limit(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db, stock="1", credit="25")
    orders = [
        await create_order(
            catalog_db,
            customer,
            location,
            [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "1"}],
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(confirm(catalog_db, order) for order in orders), return_exceptions=True
    )
    assert sum(isinstance(result, ApiError) for result in results) == 1
    assert sum(isinstance(result, dict) for result in results) == 1
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
        assert balance["committed"] == 1
        count = await catalog.session.scalar(
            select(sales_db.reservations.c.reservation_id)
            .where(sales_db.reservations.c.tenant_id == catalog.tenant_id)
            .limit(1)
        )
        assert count is not None


@pytest.mark.asyncio
async def test_concurrent_credit_limit_with_plenty_of_stock(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db, stock="10", credit="30")
    orders = [
        await create_order(
            catalog_db,
            customer,
            location,
            [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "1"}],
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(confirm(catalog_db, order) for order in orders), return_exceptions=True
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, ApiError)] == [
        "customer_credit_exceeded"
    ]
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
        assert balance["committed"] == 1


@pytest.mark.asyncio
async def test_customer_csv_preview_and_import(catalog_db):
    source = (
        "code,name,party_type,gstin,email,phone,tax_treatment,place_of_supply_state,"
        "payment_terms_days,credit_limit\n"
        "B-001,First buyer,B2B,,first@example.com,,REGISTERED,27,30,500\n"
        "B-002,Second buyer,B2C,,,,,,0,100\n"
    )
    async with catalog_db["catalog"]() as catalog:
        customers = CustomerService(catalog.session, catalog.context)
        preview = await customers.validate_csv(source)
        assert preview.can_import and preview.valid_count == 2
        assert (await customers.customers(1, 10))["total"] == 0
        await catalog.session.rollback()
    async with catalog_db["catalog"]() as catalog:
        customers = CustomerService(catalog.session, catalog.context)
        await customers.import_csv(source)
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        customers = CustomerService(catalog.session, catalog.context)
        assert (await customers.customers(1, 10))["total"] == 2
        duplicate = await customers.validate_csv(source)
        assert not duplicate.can_import and len(duplicate.issues) == 2


@pytest.mark.asyncio
async def test_kit_uses_component_snapshot(catalog_db):
    items, pcs, kind, location, customer = await setup(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        kit = await catalog.save_item(
            ItemIn(
                sku="SALE-KIT",
                name="Two soaps",
                item_kind="COMPOSITE",
                stock_uom_id=pcs,
                item_type_id=kind,
                selling_price="80",
                components=[
                    {"component_item_id": items[0]["item_id"], "quantity": "2", "uom_id": pcs}
                ],
            )
        )
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        available = await SalesOrderService(catalog.session, catalog.context).availability(
            kit["item_id"], location["location_id"]
        )
        assert available["available_stock_units"] == 5
    order = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": kit["item_id"], "uom_id": pcs, "quantity": "3"}],
    )
    confirmed = await confirm(catalog_db, order)
    assert confirmed["reservations"][0]["item_id"] == items[0]["item_id"]
    assert confirmed["reservations"][0]["quantity"] == 6
    async with catalog_db["catalog"]() as catalog:
        revised = await catalog.save_item(
            ItemUpdate(
                sku="SALE-KIT",
                name="Three soaps",
                item_kind="COMPOSITE",
                stock_uom_id=pcs,
                item_type_id=kind,
                selling_price="80",
                expected_version=kit["catalog_version"],
                components=[
                    {"component_item_id": items[0]["item_id"], "quantity": "3", "uom_id": pcs}
                ],
            ),
            kit["item_id"],
        )
        assert revised["catalog_version"] == kit["catalog_version"] + 1
        await catalog.session.commit()
    async with catalog_db["catalog"]() as catalog:
        assert (
            await SalesOrderService(catalog.session, catalog.context).availability(
                kit["item_id"], location["location_id"]
            )
        )["available_stock_units"] == 1
        stored = await SalesOrderService(catalog.session, catalog.context).order(
            order["sales_order_id"]
        )
        assert stored["reservations"][0]["quantity"] == 6


@pytest.mark.asyncio
async def test_sales_http_contract_on_isolated_database(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
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
                created = await client.post(
                    "/api/v1/sales/orders",
                    json={
                        "customer_id": str(customer["customer_id"]),
                        "location_id": str(location["location_id"]),
                        "order_date": date.today().isoformat(),
                        "lines": [
                            {
                                "item_id": str(items[0]["item_id"]),
                                "uom_id": str(pcs),
                                "quantity": "3",
                            }
                        ],
                    },
                )
                assert created.status_code == 201, created.json()
                order = created.json()
                confirmed = await client.post(
                    f"/api/v1/sales/orders/{order['sales_order_id']}/confirm",
                    json={
                        "expected_revision": order["revision"],
                        "idempotency_key": uuid.uuid4().hex,
                    },
                )
                assert confirmed.status_code == 200, confirmed.json()
                assert confirmed.json()["status"] == "CONFIRMED"
                availability = await client.get(
                    "/api/v1/sales/availability",
                    params={
                        "item_id": str(items[0]["item_id"]),
                        "location_id": str(location["location_id"]),
                    },
                )
                assert availability.status_code == 200, availability.json()
                assert Decimal(availability.json()["available_stock_units"]) == 7
                template = await client.get("/api/v1/customers/imports/template")
                assert template.status_code == 200
                assert template.text.startswith("code,name,party_type")
        finally:
            app.dependency_overrides.pop(catalog_context, None)
            app.dependency_overrides.pop(get_session, None)
