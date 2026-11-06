import asyncio
import os
import uuid
from datetime import date
from decimal import Decimal

import pytest
from app.api.v1.catalog import commit_operation
from app.core.errors import ApiError
from app.models import catalog as catalog_tables
from app.models import inventory as db
from app.schemas.catalog import ItemIn
from app.schemas.inventory import LocationIn, OpeningIn, OpeningUpdate
from app.services.inventory import InventoryService
from app.services.opening_import import OpeningImporter
from app.services.opening_stock import OpeningStockService
from app.services.outbox import OutboxWorker
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def draft(state, *, valuation_method=None, quantity="10", cost="100"):
    item, pcs, cartons, kind = await state["make_item"](valuation_method=valuation_method)
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        location = await opening.create_location(LocationIn(code="MAIN", name="Main warehouse"))
        payload = OpeningIn(
            business_date=date.today(),
            lines=[
                {
                    "item_id": item["item_id"],
                    "location_id": location["location_id"],
                    "uom_id": pcs,
                    "quantity": quantity,
                    "unit_cost": cost,
                }
            ],
        )
        document = await commit_operation(catalog.session, opening.save(payload))
    return item, location, payload, document


async def post(state, document, key=None):
    async with state["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        return await commit_operation(
            catalog.session,
            opening.post(
                document["opening_stock_id"], document["version"], key or uuid.uuid4().hex
            ),
        )


@pytest.mark.asyncio
async def test_draft_has_no_stock_effect_and_post_is_exactly_once(catalog_db):
    item, location, _, document = await draft(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        assert (
            await catalog.session.scalar(
                select(func.count())
                .select_from(db.ledger)
                .where(db.ledger.c.tenant_id == catalog.tenant_id)
            )
            == 0
        )
    key = uuid.uuid4().hex
    posted = await post(catalog_db, document, key)
    replayed = await post(catalog_db, document, key)
    assert posted == replayed
    assert posted["status"] == "POSTED"
    async with catalog_db["catalog"]() as catalog:
        row = (
            (
                await catalog.session.execute(
                    select(db.balances).where(db.balances.c.tenant_id == catalog.tenant_id)
                )
            )
            .mappings()
            .one()
        )
        assert row["physical_on_hand"] == row["accounting_on_hand"] == Decimal(10)
        assert row["inventory_value"] == Decimal(1000)
        assert (
            await catalog.session.scalar(
                select(func.count())
                .select_from(db.ledger)
                .where(db.ledger.c.tenant_id == catalog.tenant_id)
            )
            == 2
        )
        assert (
            await catalog.session.scalar(
                select(func.count())
                .select_from(db.outbox_events)
                .where(db.outbox_events.c.tenant_id == catalog.tenant_id)
            )
            == 1
        )
        locked = await catalog.row(catalog_tables.items, "item_id", item["item_id"])
        assert locked["valuation_locked_at"] is not None
        assert locked["valuation_method"] == "WEIGHTED_AVERAGE"


@pytest.mark.asyncio
async def test_post_rejects_idempotency_key_reuse_with_changed_input(catalog_db):
    _, _, _, document = await draft(catalog_db)
    key = uuid.uuid4().hex
    await post(catalog_db, document, key)
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_operation(
                catalog.session,
                opening.post(document["opening_stock_id"], document["version"] + 1, key),
            )
        assert failure.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_multiple_openings_weighted_average_is_110_not_double_counted(catalog_db):
    _, _, payload, first = await draft(catalog_db)
    await post(catalog_db, first)
    changed = payload.model_dump()
    changed["lines"][0]["unit_cost"] = Decimal(120)
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        second = await commit_operation(catalog.session, opening.save(OpeningIn(**changed)))
    await post(catalog_db, second)
    async with catalog_db["catalog"]() as catalog:
        balance = (
            (
                await catalog.session.execute(
                    select(db.balances).where(db.balances.c.tenant_id == catalog.tenant_id)
                )
            )
            .mappings()
            .one()
        )
        assert balance["physical_on_hand"] == balance["accounting_on_hand"] == Decimal(20)
        assert balance["avg_unit_cost"] == Decimal(110)
        assert balance["inventory_value"] == Decimal(2200)


@pytest.mark.asyncio
async def test_fifo_opening_creates_one_layer_not_two(catalog_db):
    _, _, _, document = await draft(catalog_db, valuation_method="FIFO")
    await post(catalog_db, document)
    async with catalog_db["catalog"]() as catalog:
        layers = (
            (
                await catalog.session.execute(
                    select(db.fifo_layers).where(db.fifo_layers.c.tenant_id == catalog.tenant_id)
                )
            )
            .mappings()
            .all()
        )
        assert len(layers) == 1
        assert layers[0]["remaining_qty"] == Decimal(10)
        inventory = InventoryService(catalog.session, catalog.context)
        row = (await inventory.balances(1, 25))["items"][0]
        assert row["inventory_value"] == Decimal(1000) and row["valuation_complete"]


@pytest.mark.asyncio
async def test_late_multiline_failure_rolls_back_ledger_balances_marker_receipt_and_outbox(
    catalog_db, monkeypatch
):
    item, location, payload, _ = await draft(catalog_db, valuation_method="FIFO")
    async with catalog_db["catalog"]() as catalog:
        other = await catalog.save_item(
            ItemIn(
                sku="OTHER",
                name="Other",
                item_type_id=item["item_type_id"],
                stock_uom_id=item["stock_uom_id"],
                valuation_method="FIFO",
            )
        )
        changed = payload.model_dump()
        changed["lines"].append({**changed["lines"][0], "item_id": other["item_id"]})
        opening = OpeningStockService(catalog.session, catalog.context)
        document = await commit_operation(catalog.session, opening.save(OpeningIn(**changed)))
    key = uuid.uuid4().hex
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        original = opening.posting.append_opening
        count = 0

        async def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("injected late posting failure")
            await original(*args, **kwargs)

        monkeypatch.setattr(opening.posting, "append_opening", fail_second)
        with pytest.raises(RuntimeError):
            await commit_operation(
                catalog.session, opening.post(document["opening_stock_id"], 1, key)
            )
    async with catalog_db["catalog"]() as catalog:
        for table in [
            db.ledger,
            db.balances,
            db.fifo_layers,
            db.outbox_events,
            db.idempotency_keys,
        ]:
            assert (
                await catalog.session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.tenant_id == catalog.tenant_id)
                )
                == 0
            )
        record = await catalog.row(catalog_tables.items, "item_id", item["item_id"])
        assert record["valuation_locked_at"] is None
    assert (await post(catalog_db, document, key))["status"] == "POSTED"


@pytest.mark.asyncio
async def test_database_rejects_posted_header_and_line_edits(catalog_db):
    _, _, _, document = await draft(catalog_db)
    await post(catalog_db, document)
    for statement, values in [
        (
            "UPDATE cims.opening_stock SET notes='changed' WHERE opening_stock_id=:id",
            {"id": document["opening_stock_id"]},
        ),
        (
            "DELETE FROM cims.opening_stock_line WHERE opening_stock_id=:id",
            {"id": document["opening_stock_id"]},
        ),
    ]:
        async with catalog_db["catalog"]() as catalog:
            with pytest.raises(SQLAlchemyError) as failure:
                await catalog.session.execute(text(statement), values)
            assert getattr(failure.value.orig, "sqlstate", None) == "55000"


@pytest.mark.asyncio
async def test_stale_draft_edit_and_catalog_change_are_rejected(catalog_db):
    item, _, payload, document = await draft(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        opening = OpeningStockService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_operation(
                catalog.session,
                opening.save(
                    OpeningUpdate(**payload.model_dump(), expected_version=9),
                    document["opening_stock_id"],
                ),
            )
        assert failure.value.code == "stale_opening_stock"
    async with catalog_db["admin"].begin() as connection:
        await connection.execute(
            text("UPDATE cims.item SET catalog_version=catalog_version+1 WHERE item_id=:id"),
            {"id": item["item_id"]},
        )
    with pytest.raises(ApiError) as failure:
        await post(catalog_db, document)
    assert failure.value.code == "opening_catalog_changed"


@pytest.mark.asyncio
async def test_concurrent_identical_post_has_one_effect(catalog_db):
    _, _, _, document = await draft(catalog_db)
    key = uuid.uuid4().hex
    first, second = await asyncio.gather(
        post(catalog_db, document, key), post(catalog_db, document, key)
    )
    assert first == second
    async with catalog_db["catalog"]() as catalog:
        count = await catalog.session.scalar(
            select(func.count())
            .select_from(db.ledger)
            .where(db.ledger.c.tenant_id == catalog.tenant_id)
        )
        assert count == 2


@pytest.mark.asyncio
async def test_csv_validation_and_draft_create_never_post_stock(catalog_db):
    _, _, _, _ = await draft(catalog_db)
    source = "sku,location,unit,quantity,unit_cost\nSOAP,MAIN,CTN,10,100\n"
    async with catalog_db["catalog"]() as catalog:
        importer = OpeningImporter(OpeningStockService(catalog.session, catalog.context))
        preview, _ = await importer.validate(source, date.today())
        assert preview.can_import and Decimal(preview.rows[0]["stock_quantity"]) == Decimal(480)
        document = await commit_operation(
            catalog.session, importer.create_draft(source, date.today())
        )
        assert document["status"] == "DRAFT"
    async with catalog_db["catalog"]() as catalog:
        importer = OpeningImporter(OpeningStockService(catalog.session, catalog.context))
        preview, _ = await importer.validate(source + "SOAP,MAIN,PCS,1,100\n", date.today())
        assert not preview.can_import and preview.issues[0].row == 3
        assert (
            await catalog.session.scalar(
                select(func.count())
                .select_from(db.ledger)
                .where(db.ledger.c.tenant_id == catalog.tenant_id)
            )
            == 0
        )
        with pytest.raises(ApiError):
            await commit_operation(
                catalog.session,
                importer.create_draft(source + "MISSING,MAIN,PCS,1,100\n", date.today()),
            )
    assert (await post(catalog_db, document))["status"] == "POSTED"


@pytest.mark.asyncio
async def test_worker_claims_once_and_reports_reconciliation_drift(catalog_db):
    _, _, payload, document = await draft(catalog_db)
    await post(catalog_db, document)
    async with catalog_db["catalog"]() as catalog:
        result = await commit_operation(
            catalog.session, OutboxWorker(catalog.session, catalog.context).run_once()
        )
        assert result["status"] == "DONE"
    async with catalog_db["catalog"]() as catalog:
        assert await OutboxWorker(catalog.session, catalog.context).run_once() is None
        second = await commit_operation(
            catalog.session, OpeningStockService(catalog.session, catalog.context).save(payload)
        )
    await post(catalog_db, second)
    async with catalog_db["admin"].begin() as connection:
        await connection.execute(
            text(
                "UPDATE cims.stock_balance SET physical_on_hand=physical_on_hand+1 "
                "WHERE tenant_id=:tenant"
            ),
            {"tenant": catalog_db["tenants"][0]},
        )
    async with catalog_db["catalog"]() as catalog:
        result = await commit_operation(
            catalog.session, OutboxWorker(catalog.session, catalog.context).run_once()
        )
        assert (
            result["status"] == "FAILED" and result["error_code"] == "stock_reconciliation_failed"
        )
    async with catalog_db["catalog"]() as catalog:
        # Failed jobs back off; the next poll must not immediately retry the same event.
        assert await OutboxWorker(catalog.session, catalog.context).run_once() is None


@pytest.mark.asyncio
async def test_changed_location_type_is_revalidated_before_post(catalog_db):
    _, location, _, document = await draft(catalog_db)
    async with catalog_db["admin"].begin() as connection:
        await connection.execute(
            text("UPDATE cims.location_type SET is_storage=false WHERE location_type_id=:id"),
            {"id": location["location_type_id"]},
        )
    with pytest.raises(ApiError) as failure:
        await post(catalog_db, document)
    assert failure.value.code == "opening_location_not_storage"


@pytest.mark.asyncio
async def test_operational_history_blocks_opening_and_ledger_mutation_is_denied(catalog_db):
    item, location, _, document = await draft(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        movement = await OpeningStockService(
            catalog.session, catalog.context
        ).posting.opening_movement_type("PHYSICAL")
        await catalog.session.execute(
            text(
                "INSERT INTO cims.stock_ledger_entry(occurred_at,tenant_id,item_id,"
                "location_id,stock_state,"
                "movement_type_id,direction,quantity,unit_cost,source_document_type) "
                "VALUES(clock_timestamp(),:tenant,:item,:location,'PHYSICAL',:movement,'IN',1,100,"
                "'PURCHASE_RECEIVE')"
            ),
            {
                "tenant": catalog.tenant_id,
                "item": item["item_id"],
                "location": location["location_id"],
                "movement": movement,
            },
        )
        await catalog.session.commit()
    with pytest.raises(ApiError) as failure:
        await post(catalog_db, document)
    assert failure.value.code == "opening_after_operations"
    for statement in [
        "UPDATE cims.stock_ledger_entry SET quantity=99 WHERE tenant_id=:tenant",
        "DELETE FROM cims.stock_ledger_entry WHERE tenant_id=:tenant",
        "TRUNCATE cims.stock_ledger_entry",
        "TRUNCATE cims.opening_stock_line",
    ]:
        async with catalog_db["admin"].begin() as connection:
            with pytest.raises(SQLAlchemyError) as failure:
                await connection.execute(text(statement), {"tenant": catalog_db["tenants"][0]})
            assert getattr(failure.value.orig, "sqlstate", None) == "55000"
