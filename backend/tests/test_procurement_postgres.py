"""Supplier acceptance checks against the isolated, restricted PostgreSQL role."""

import os
from decimal import Decimal

import pytest
from app.api.catalog_access import catalog_context
from app.api.v1.procurement import commit_procurement
from app.core.errors import ApiError
from app.db.base import get_session
from app.main import app
from app.models import procurement as db
from app.schemas.procurement import (
    PreferredSupplierIn,
    SupplierImportPreview,
    SupplierIn,
    SupplierOut,
    SupplierUpdate,
)
from app.services.procurement import ProcurementService
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


async def create_supplier(state, code="SUP-001", **changes):
    async with state["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        payload = SupplierIn(code=code, name="Supplier one", **changes)
        return await commit_procurement(catalog.session, service.save_supplier(payload))


@pytest.mark.asyncio
async def test_supplier_create_search_duplicate_and_tenant_scope(catalog_db):
    created = await create_supplier(catalog_db, lead_time_days=7, payment_terms_days=30)
    assert SupplierOut.model_validate(created).code == "SUP-001"
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        listing = await service.suppliers(1, 25, "supplier")
        assert listing["total"] == 1 and listing["items"][0]["vendor_id"] == created["vendor_id"]
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(
                catalog.session, service.save_supplier(SupplierIn(code="sup-001", name="Duplicate"))
            )
        assert failure.value.code == "supplier_code_exists"
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        assert (await service.suppliers(1, 25))["total"] == 0
        # The restricted login cannot see another tenant even without an API filter.
        assert await catalog.session.scalar(select(func.count()).select_from(db.vendors)) == 0


@pytest.mark.asyncio
async def test_supplier_edit_rejects_stale_timestamp(catalog_db):
    created = await create_supplier(catalog_db)
    original = SupplierOut.model_validate(created)
    changed = SupplierUpdate(
        **{
            "code": original.code,
            "name": "Supplier renamed",
            "expected_updated_at": original.updated_at,
        }
    )
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        updated = await commit_procurement(
            catalog.session, service.save_supplier(changed, original.vendor_id)
        )
    assert updated["name"] == "Supplier renamed"
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(
                catalog.session, service.save_supplier(changed, original.vendor_id)
            )
        assert failure.value.code == "stale_supplier"


@pytest.mark.asyncio
async def test_supplier_csv_preview_import_and_duplicate_report(catalog_db):
    source = (
        "code,name,gstin,email,phone,lead_time_days,payment_terms_days\n"
        "SUP-A,Supplier A,,a@example.com,,7,30\n"
        "SUP-B,Supplier B,,,,14,0\n"
    )
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        preview = await service.validate_supplier_csv(source)
        assert preview.can_import and preview.valid_count == 2
        assert await catalog.session.scalar(select(func.count()).select_from(db.vendors)) == 0
        result = await commit_procurement(catalog.session, service.import_suppliers(source))
        assert result.can_import and result.row_count == 2
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        assert (await service.suppliers(1, 25))["total"] == 2
        duplicate = await service.validate_supplier_csv(source)
        assert not duplicate.can_import and duplicate.issues[0].row == 2


@pytest.mark.asyncio
async def test_late_csv_unique_failure_rolls_back_first_supplier(catalog_db, monkeypatch):
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        same = SupplierIn(code="ROLLBACK", name="Rollback supplier")

        async def duplicate_preview(_source):
            return SupplierImportPreview(
                row_count=2, valid_count=2, can_import=True, issues=[], rows=[same, same]
            )

        monkeypatch.setattr(service, "validate_supplier_csv", duplicate_preview)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(catalog.session, service.import_suppliers("test"))
        assert failure.value.code == "procurement_duplicate"
    async with catalog_db["catalog"]() as catalog:
        assert await catalog.session.scalar(select(func.count()).select_from(db.vendors)) == 0


@pytest.mark.asyncio
async def test_preferred_supplier_reassignment_keeps_one_preference(catalog_db):
    item, *_ = await catalog_db["make_item"]()
    first = await create_supplier(catalog_db, "FIRST")
    second = await create_supplier(catalog_db, "SECOND")
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        selected = await commit_procurement(
            catalog.session,
            service.set_preferred_supplier(
                PreferredSupplierIn(
                    item_id=item["item_id"],
                    vendor_id=first["vendor_id"],
                    unit_price="20.123456",
                    min_order_qty="5",
                    lead_time_days=7,
                )
            ),
        )
        assert selected["vendor_code"] == "FIRST"
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        await commit_procurement(
            catalog.session,
            service.set_preferred_supplier(
                PreferredSupplierIn(item_id=item["item_id"], vendor_id=second["vendor_id"])
            ),
        )
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        listing = await service.preferred_suppliers(1, 25)
        assert listing["total"] == 1 and listing["items"][0]["vendor_code"] == "SECOND"
        assert await catalog.session.scalar(select(func.count()).select_from(db.item_vendors)) == 2
        first_link = await catalog.session.scalar(
            select(db.item_vendors.c.unit_price).where(
                db.item_vendors.c.vendor_id == first["vendor_id"]
            )
        )
        assert first_link == Decimal("20.123456")


@pytest.mark.asyncio
async def test_preferred_supplier_rejects_cross_tenant_vendor(catalog_db):
    item, *_ = await catalog_db["make_item"]()
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        other = await commit_procurement(
            catalog.session,
            service.save_supplier(SupplierIn(code="OTHER", name="Other workspace supplier")),
        )
    async with catalog_db["catalog"]() as catalog:
        service = ProcurementService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as failure:
            await commit_procurement(
                catalog.session,
                service.set_preferred_supplier(
                    PreferredSupplierIn(item_id=item["item_id"], vendor_id=other["vendor_id"])
                ),
            )
        assert failure.value.code == "invalid_catalog_reference"


@pytest.mark.asyncio
async def test_supplier_http_contract_on_isolated_database(catalog_db):
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
                    "/api/v1/procurement/suppliers",
                    json={
                        "code": "HTTP-SUP",
                        "name": "HTTP supplier",
                        "lead_time_days": 5,
                    },
                )
                assert created.status_code == 201, created.json()
                assert created.json()["code"] == "HTTP-SUP"
                listing = await client.get(
                    "/api/v1/procurement/suppliers", params={"search": "HTTP"}
                )
                assert listing.status_code == 200 and listing.json()["total"] == 1
                template = await client.get("/api/v1/procurement/suppliers/imports/template")
                assert template.status_code == 200
                assert template.text.startswith("code,name,gstin")
                preview = await client.post(
                    "/api/v1/procurement/suppliers/imports/validate",
                    json={
                        "csv_text": "code,name,gstin,email,phone,"
                        "lead_time_days,payment_terms_days\n"
                        "HTTP-2,Other supplier,,,,3,0\n",
                    },
                )
                assert preview.status_code == 200 and preview.json()["can_import"]
        finally:
            app.dependency_overrides.pop(catalog_context, None)
            app.dependency_overrides.pop(get_session, None)
