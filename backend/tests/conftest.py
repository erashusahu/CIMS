"""Shared fixture for the disposable PostgreSQL server on port 55439."""

import os
import uuid
from contextlib import asynccontextmanager

import pytest_asyncio
from app.api.catalog_access import CatalogContext
from app.api.v1.catalog import commit_operation
from app.models import catalog as db
from app.schemas.catalog import ItemIn, ItemTypeIn, UnitIn
from app.services.catalog import CatalogService
from sqlalchemy import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_URL = os.getenv("CIMS_CATALOG_TEST_URL")


@pytest_asyncio.fixture
async def catalog_db():
    url = make_url(TEST_URL)
    # Refuse the existing local server even if an environment variable is misconfigured.
    assert url.host == "127.0.0.1" and url.port == 55439
    assert url.database == "Inventroy" and url.username == "cims_catalog_test_app"
    app_engine = create_async_engine(url, pool_size=5)
    admin_engine = create_async_engine(url.set(username="cims_test_admin"))
    sessions = async_sessionmaker(app_engine, expire_on_commit=False)
    tenants = [uuid.uuid4(), uuid.uuid4()]
    async with admin_engine.begin() as connection:
        for tenant in tenants:
            await connection.execute(
                insert(db.tenants).values(
                    tenant_id=tenant,
                    name="Catalog test",
                    slug=f"test-{tenant.hex}",
                    status="ACTIVE",
                    default_valuation_method="WEIGHTED_AVERAGE",
                )
            )

    @asynccontextmanager
    async def catalog(tenant=tenants[0]):
        async with sessions() as session:
            context = CatalogContext(tenant, None, "Owner", "isolated-catalog-test", local=True)
            service = CatalogService(session, context)
            await service.prepare()
            yield service

    async def create_units(tenant=tenants[0]):
        async with catalog(tenant) as service:
            pcs = await service.create_unit(UnitIn(code="PCS", name="Pieces"))
            cartons = await service.create_unit(UnitIn(code="CTN", name="Cartons"))
            item_type = await service.create_type(ItemTypeIn(code="GOODS", name="Goods"))
            await service.session.commit()
        return pcs["uom_id"], cartons["uom_id"], item_type["item_type_id"]

    async def make_item(sku="SOAP", *, tenant=tenants[0], **changes):
        pcs, cartons, kind = await create_units(tenant)
        payload = ItemIn(
            sku=sku,
            name="Soap",
            stock_uom_id=pcs,
            item_type_id=kind,
            purchase_uom_id=cartons,
            conversions=[{"from_uom_id": cartons, "factor": "48"}],
            cost_price="20",
            selling_price="25",
            **changes,
        )
        async with catalog(tenant) as service:
            item = await commit_operation(service.session, service.save_item(payload))
        return item, pcs, cartons, kind

    yield {
        "catalog": catalog,
        "create_units": create_units,
        "make_item": make_item,
        "admin": admin_engine,
        "app": app_engine,
        "sessions": sessions,
        "tenants": tenants,
    }
    await app_engine.dispose()
    await admin_engine.dispose()
