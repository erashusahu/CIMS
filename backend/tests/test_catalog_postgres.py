"""Acceptance checks against the disposable cluster, never the user's server on 5432."""

import asyncio
import os
from decimal import Decimal

import pytest
from app.api.catalog_access import CatalogContext
from app.api.v1.catalog import commit_operation
from app.core.errors import ApiError
from app.models import catalog as db
from app.schemas.catalog import GroupIn, ItemIn, ItemUpdate
from app.services.catalog import CatalogService
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_URL = os.getenv("CIMS_CATALOG_TEST_URL")
pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="Set CIMS_CATALOG_TEST_URL for the isolated cluster"
)


def update_payload(item: dict, **changes):
    names = set(ItemIn.model_fields)
    data = {key: value for key, value in item.items() if key in names}
    data.update(changes)
    return ItemUpdate(**data, expected_version=item["catalog_version"])


@pytest.mark.asyncio
async def test_restricted_role_and_case_insensitive_tenant_uniqueness(catalog_db):
    state = catalog_db
    item, pcs, cartons, kind = await state["make_item"](barcode="AB123")
    async with state["catalog"]() as service:
        role = (
            await service.session.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not role.rolsuper and not role.rolbypassrls
        duplicate = ItemIn(sku="soap", name="Other", stock_uom_id=pcs, item_type_id=kind)
        with pytest.raises(ApiError) as failure:
            await commit_operation(service.session, service.save_item(duplicate))
        assert failure.value.status_code == 409
    async with state["catalog"]() as service:
        duplicate = ItemIn(
            sku="OTHER", name="Other", barcode="ab123", stock_uom_id=pcs, item_type_id=kind
        )
        with pytest.raises(ApiError) as failure:
            await commit_operation(service.session, service.save_item(duplicate))
        assert failure.value.status_code == 409
    other, *_ = await state["make_item"](sku="SOAP", barcode="AB123", tenant=state["tenants"][1])
    assert other["item_id"] != item["item_id"]


@pytest.mark.asyncio
async def test_real_conversion_and_fractional_rejection(catalog_db):
    item, pcs, cartons, _ = await catalog_db["make_item"]()
    async with catalog_db["catalog"]() as service:
        converted = await service.convert(item["item_id"], cartons, Decimal("10"))
        assert converted["stock_quantity"] == Decimal("480")
        with pytest.raises(ApiError) as failure:
            await service.convert(item["item_id"], cartons, Decimal("1.5"))
        assert failure.value.code == "fractional_quantity"


@pytest.mark.asyncio
async def test_variant_and_virtual_kit_are_real_records_without_stock_changes(catalog_db):
    state = catalog_db
    soap, pcs, _, kind = await state["make_item"]()
    async with state["catalog"]() as service:
        group = await service.create_group(
            GroupIn(code="SOAP-SIZES", name="Soap sizes", attributes=["Size"])
        )
        variant = await service.save_item(
            ItemIn(
                sku="SOAP-100",
                name="Soap 100g",
                item_kind="VARIANT",
                item_group_id=group["item_group_id"],
                attributes={"Size": "100g"},
                stock_uom_id=pcs,
                item_type_id=kind,
            )
        )
        kit = await service.save_item(
            ItemIn(
                sku="SOAP-KIT",
                name="Soap pair",
                item_kind="COMPOSITE",
                stock_uom_id=pcs,
                item_type_id=kind,
                components=[{"component_item_id": soap["item_id"], "quantity": "2", "uom_id": pcs}],
            )
        )
        await service.session.commit()
    async with state["catalog"]() as service:
        stored = await service.item_details(variant["item_id"])
        assert stored["attributes"] == {"Size": "100g"}
        assert (await service.item_details(kit["item_id"]))["components"][0]["quantity"] == Decimal(
            2
        )
        for name in ["stock_balance", "stock_ledger_entry"]:
            count = await service.session.scalar(
                text(f"SELECT count(*) FROM cims.{name} WHERE tenant_id=:tenant"),
                {"tenant": service.tenant_id},
            )
            assert count == 0


@pytest.mark.asyncio
async def test_cross_tenant_ids_are_hidden_by_queries_and_postgres_rls(catalog_db):
    state = catalog_db
    other, *_ = await state["make_item"](tenant=state["tenants"][1])
    async with state["catalog"]() as service:
        with pytest.raises(ApiError) as failure:
            await service.item_details(other["item_id"])
        assert failure.value.status_code == 404
        # Deliberately omit the service's tenant filter: the database must still hide it.
        rows = await service.session.execute(
            select(db.items.c.item_id).where(db.items.c.item_id == other["item_id"])
        )
        assert rows.all() == []


@pytest.mark.asyncio
async def test_real_stale_edit_protection(catalog_db):
    item, *_ = await catalog_db["make_item"]()
    async with catalog_db["catalog"]() as service:
        changed = await commit_operation(
            service.session,
            service.save_item(update_payload(item, name="New name"), item["item_id"]),
        )
    assert changed["catalog_version"] == item["catalog_version"] + 1
    async with catalog_db["catalog"]() as service:
        with pytest.raises(ApiError) as failure:
            await commit_operation(
                service.session,
                service.save_item(update_payload(item, name="Stale overwrite"), item["item_id"]),
            )
        assert failure.value.code == "stale_catalog_item"


@pytest.mark.asyncio
async def test_concurrent_kit_updates_cannot_create_a_cycle(catalog_db):
    state = catalog_db
    soap, pcs, _, kind = await state["make_item"]()
    async with state["catalog"]() as service:
        kits = []
        for sku in ["KIT-A", "KIT-B"]:
            kits.append(
                await service.save_item(
                    ItemIn(
                        sku=sku,
                        name=sku,
                        item_kind="COMPOSITE",
                        stock_uom_id=pcs,
                        item_type_id=kind,
                        components=[
                            {"component_item_id": soap["item_id"], "quantity": "1", "uom_id": pcs}
                        ],
                    )
                )
            )
        await service.session.commit()

    async def edit(parent, child):
        async with state["catalog"]() as service:
            payload = update_payload(
                parent,
                components=[
                    {
                        "component_item_id": child["item_id"],
                        "quantity": "1",
                        "uom_id": pcs,
                    }
                ],
            )
            try:
                await commit_operation(
                    service.session, service.save_item(payload, parent["item_id"])
                )
                return "saved"
            except ApiError as failure:
                return failure.code

    outcomes = await asyncio.wait_for(
        asyncio.gather(edit(kits[0], kits[1]), edit(kits[1], kits[0])), timeout=10
    )
    assert sorted(outcomes) == ["kit_cycle", "saved"]


@pytest.mark.asyncio
async def test_csv_preview_commit_and_late_failure_rollback(catalog_db, monkeypatch):
    state = catalog_db
    await state["create_units"]()
    source = (
        "sku,name,stock_unit,item_type,purchase_unit,purchase_factor\n"
        "CSV-SOAP,Soap,PCS,GOODS,CTN,48\nCSV-OTHER,Other,PCS,GOODS,PCS,1\n"
    )
    async with state["catalog"]() as service:
        preview, _ = await service.validate_csv(source)
        assert preview.can_import and preview.valid_count == 2
        count = await service.session.scalar(
            select(func.count()).select_from(db.items).where(service.scope(db.items))
        )
        assert count == 0
        await service.session.rollback()
    async with state["catalog"]() as service:
        original_save = service.save_item
        calls = 0

        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated failure after the first item insert")
            return await original_save(*args, **kwargs)

        monkeypatch.setattr(service, "save_item", fail_second)
        with pytest.raises(RuntimeError):
            await commit_operation(service.session, service.commit_csv(source))
    async with state["catalog"]() as service:
        count = await service.session.scalar(
            select(func.count()).select_from(db.items).where(service.scope(db.items))
        )
        assert count == 0
        result = await commit_operation(service.session, service.commit_csv(source))
        assert result["created"] == 2
    async with state["catalog"]() as service:
        preview, _ = await service.validate_csv(source)
        assert not preview.can_import
        assert {issue.field for issue in preview.issues} == {"sku"}


@pytest.mark.asyncio
async def test_transaction_local_tenant_context_does_not_leak(catalog_db):
    state = catalog_db
    item, *_ = await state["make_item"]()
    engine = create_async_engine(make_url(TEST_URL), pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine)
    try:
        for tenant, expected in [(state["tenants"][0], 1), (state["tenants"][1], 0)]:
            async with sessions() as session:
                service = CatalogService(
                    session, CatalogContext(tenant, None, "Owner", "pool-test", local=True)
                )
                await service.prepare()
                visible = await session.scalar(
                    select(func.count())
                    .select_from(db.items)
                    .where(db.items.c.item_id == item["item_id"])
                )
                assert visible == expected
                await session.commit()
        async with sessions() as session:
            visible = await session.scalar(
                select(func.count())
                .select_from(db.items)
                .where(db.items.c.item_id == item["item_id"])
            )
            assert visible == 0
    finally:
        await engine.dispose()
