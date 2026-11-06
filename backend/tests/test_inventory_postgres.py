"""Read-model checks on the isolated PostgreSQL fixture, with no writes to port 5432."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.services.inventory import InventoryService
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def seed_movements(state, *, fifo=False):
    item, *_ = await state["make_item"]()
    tenant = state["tenants"][0]
    location, location_type = uuid.uuid4(), uuid.uuid4()
    movement_in, movement_out = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with state["admin"].begin() as connection:
        await connection.execute(text("SET LOCAL search_path = cims, pg_catalog, public"))
        await connection.execute(
            text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": str(tenant)}
        )
        await connection.execute(
            text(
                "INSERT INTO cims.location_type(location_type_id,tenant_id,code,name) "
                "VALUES(:id,:tenant,'WAREHOUSE','Warehouse')"
            ),
            {"id": location_type, "tenant": tenant},
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "cims.location(location_id,tenant_id,location_type_id,code,name) VALUES(:id,:tenant"
                ",:type,'MAIN','Main warehouse')"
            ),
            {"id": location, "tenant": tenant, "type": location_type},
        )
        await connection.execute(
            text(
                "INSERT INTO "
                "cims.movement_type(movement_type_id,tenant_id,code,name,stock_state,direction_hint"
                ") VALUES(:id,:tenant,'TEST-IN','Test receipt','PHYSICAL','IN'),(:out,:tenant,'TEST"
                "-OUT','Test issue','PHYSICAL','OUT')"
            ),
            {"id": movement_in, "out": movement_out, "tenant": tenant},
        )
        for quantity, cost, movement, direction in [
            (10, 100, movement_in, "IN"),
            (10, 120, movement_in, "IN"),
        ]:
            await connection.execute(
                text(
                    "INSERT INTO "
                    "cims.stock_ledger_entry(tenant_id,occurred_at,item_id,location_id,stock_state,"
                    "movement_type_id,direction,quantity,unit_cost) VALUES(:tenant,:at,:item,:locat"
                    "ion,'PHYSICAL',:movement,:direction,:quantity,:cost)"
                ),
                {
                    "tenant": tenant,
                    "at": now,
                    "item": item["item_id"],
                    "location": location,
                    "movement": movement,
                    "direction": direction,
                    "quantity": quantity,
                    "cost": cost,
                },
            )
        if fifo:
            await connection.execute(
                text(
                    "UPDATE cims.item SET valuation_method='FIFO' WHERE tenant_id=:tenant "
                    "AND item_id=:item"
                ),
                {"tenant": tenant, "item": item["item_id"]},
            )
            for quantity, cost in [(10, 100), (2, 120)]:
                await connection.execute(
                    text(
                        "INSERT INTO "
                        "cims.stock_ledger_entry(tenant_id,occurred_at,item_id,location_id,stock_st"
                        "ate,movement_type_id,direction,quantity,unit_cost) VALUES(:tenant,:at,:ite"
                        "m,:location,'PHYSICAL',:movement,'OUT',:quantity,:cost)"
                    ),
                    {
                        "tenant": tenant,
                        "at": now,
                        "item": item["item_id"],
                        "location": location,
                        "movement": movement_out,
                        "quantity": quantity,
                        "cost": cost,
                    },
                )
            for remaining, cost, received in [(0, 100, now - timedelta(days=1)), (8, 120, now)]:
                await connection.execute(
                    text(
                        "INSERT INTO "
                        "cims.fifo_layer(tenant_id,item_id,location_id,received_at,original_qty,rem"
                        "aining_qty,unit_cost) VALUES(:tenant,:item,:location,:at,10,:remaining,:co"
                        "st)"
                    ),
                    {
                        "tenant": tenant,
                        "item": item["item_id"],
                        "location": location,
                        "at": received,
                        "remaining": remaining,
                        "cost": cost,
                    },
                )
    return item, location


@pytest.mark.asyncio
async def test_weighted_stock_history_and_reconciliation_read_real_ledger(catalog_db):
    item, location = await seed_movements(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        service = InventoryService(catalog.session, catalog.context)
        await service.prepare()
        balances = await service.balances(1, 25)
        assert balances["total"] == 1
        record = balances["items"][0]
        assert record["physical_on_hand"] == Decimal(20)
        assert record["accounting_on_hand"] == Decimal(0)
        assert record["inventory_value"] == Decimal(2200)
        assert record["atp"] == Decimal(20)
        assert record["eligible_for_sales"] and record["valuation_complete"]
        movements = await service.movements(1, 25, item["item_id"], location, "PHYSICAL")
        assert movements["total"] == 2
        assert (await service.reconcile(1, 25))["total"] == 0


@pytest.mark.asyncio
async def test_fifo_report_uses_remaining_layers_instead_of_weighted_trigger_value(catalog_db):
    await seed_movements(catalog_db, fifo=True)
    async with catalog_db["catalog"]() as catalog:
        service = InventoryService(catalog.session, catalog.context)
        await service.prepare()
        record = (await service.balances(1, 25))["items"][0]
        assert record["physical_on_hand"] == Decimal(8)
        assert record["valuation_method"] == "FIFO"
        assert record["inventory_value"] == Decimal(960)
        legacy_value = await catalog.session.scalar(
            text("SELECT inventory_value FROM cims.stock_balance WHERE tenant_id=:tenant"),
            {"tenant": catalog.tenant_id},
        )
        assert legacy_value == Decimal(880)
        assert (await service.reconcile(1, 25))["total"] == 0


@pytest.mark.asyncio
async def test_drift_and_incomplete_fifo_are_reported_without_repairing_data(catalog_db):
    item, location = await seed_movements(catalog_db, fifo=True)
    async with catalog_db["admin"].begin() as connection:
        await connection.execute(
            text(
                "UPDATE cims.stock_balance SET physical_on_hand=9 WHERE "
                "tenant_id=:tenant AND item_id=:item AND location_id=:location"
            ),
            {"tenant": catalog_db["tenants"][0], "item": item["item_id"], "location": location},
        )
    async with catalog_db["catalog"]() as catalog:
        service = InventoryService(catalog.session, catalog.context)
        await service.prepare()
        differences = await service.reconcile(1, 25)
        assert differences["total"] == 1
        assert differences["items"][0]["physical_difference"] == Decimal(1)
        record = (await service.balances(1, 25))["items"][0]
        assert not record["valuation_complete"]
        assert record["inventory_value"] is None
        assert record["physical_on_hand"] == Decimal(9)
