"""Stage 3J overview matches tenant stock and refuses unscoped roles."""

import os
import uuid
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_purchase_workflow_postgres import setup_purchase
from test_sales_postgres import confirm, create_order, setup

from app.api.catalog_access import CatalogContext, catalog_context
from app.api.v1.reports import check_dates
from app.core.errors import ApiError
from app.db.base import get_session
from app.main import app
from app.services.reports import ReportsService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


@pytest.mark.asyncio
async def test_overview_matches_confirmed_reservation_and_tenant(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db)
    order = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "3"}],
    )
    await confirm(catalog_db, order)
    async with catalog_db["catalog"]() as catalog:
        service = ReportsService(catalog.session, catalog.context)
        await service.prepare()
        result = await service.overview()
        assert result["scope"] == "WHOLE_BUSINESS"
        assert result["active_items"] == 1
        assert result["stock_positions"] == 1
        assert result["below_safety_positions"] == 0
        assert result["confirmed_sales_orders"] == 1
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as other:
        service = ReportsService(other.session, other.context)
        await service.prepare()
        result = await service.overview()
        assert result["active_items"] == 0
        assert result["stock_positions"] == 0
        assert result["confirmed_sales_orders"] == 0


@pytest.mark.asyncio
async def test_whole_business_report_fails_closed_for_unmapped_role(catalog_db):
    async with catalog_db["catalog"]() as catalog:
        context = CatalogContext(
            catalog.context.tenant_id, uuid.uuid4(), "Warehouse", "report test"
        )
        with pytest.raises(ApiError) as denied:
            await ReportsService(catalog.session, context).prepare()
        assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_fifo_aging_and_order_summaries_use_current_tenant(catalog_db):
    items, pcs, _, location, customer = await setup(catalog_db, stock="10", valuation_method="FIFO")
    order = await create_order(
        catalog_db,
        customer,
        location,
        [{"item_id": items[0]["item_id"], "uom_id": pcs, "quantity": "2"}],
    )
    await confirm(catalog_db, order)
    async with catalog_db["catalog"]() as catalog:
        reports = ReportsService(catalog.session, catalog.context)
        await reports.prepare()
        aging = await reports.aging(1, 25)
        assert aging["total"] == 1
        assert aging["items"][0]["remaining_quantity"] == 10
        assert aging["items"][0]["remaining_value"] == 200
        assert aging["items"][0]["age_days"] >= 0
        sales = await reports.sales_summary()
        assert len(sales) == 1
        assert sales[0]["status"] == "CONFIRMED"
        assert sales[0]["order_count"] == 1
        assert sales[0]["order_value"] == 50
        tax = await reports.order_tax_support()
        assert len(tax) == 1
        assert tax[0]["taxable_value"] == 50
        assert tax[0]["estimated_tax"] == 0
        tomorrow = date.today() + timedelta(days=1)
        assert await reports.sales_summary(tomorrow) == []
        assert await reports.order_tax_support(tomorrow) == []
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as other:
        reports = ReportsService(other.session, other.context)
        await reports.prepare()
        assert (await reports.aging(1, 25))["total"] == 0
        assert await reports.sales_summary() == []


@pytest.mark.asyncio
async def test_purchase_summary_uses_order_currency_and_date(catalog_db):
    _, _, _, order = await setup_purchase(catalog_db)
    async with catalog_db["catalog"]() as catalog:
        reports = ReportsService(catalog.session, catalog.context)
        await reports.prepare()
        rows = await reports.purchase_summary()
        assert len(rows) == 1
        assert rows[0]["status"] == order["status"] == "SENT"
        assert rows[0]["order_count"] == 1
        assert rows[0]["order_value"] == order["grand_total"]
        assert rows[0]["currency"] == "INR"
        assert await reports.purchase_summary(date.today() + timedelta(days=1)) == []


def test_report_date_range_rejects_reversed_dates():
    with pytest.raises(ApiError) as invalid:
        check_dates(date(2026, 11, 7), date(2026, 11, 6))
    assert invalid.value.code == "report_date_range"


@pytest.mark.asyncio
async def test_report_http_contract_on_isolated_database(catalog_db):
    await setup(catalog_db, valuation_method="FIFO")
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
                overview = await client.get("/api/v1/reports/overview")
                assert overview.status_code == 200, overview.json()
                assert overview.json()["scope"] == "WHOLE_BUSINESS"
                aging = await client.get("/api/v1/reports/fifo-aging")
                assert aging.status_code == 200, aging.json()
                assert aging.json()["total"] == 1
                invalid = await client.get(
                    "/api/v1/reports/sales-summary",
                    params={"from_date": "2026-11-07", "to_date": "2026-11-06"},
                )
                assert invalid.status_code == 422
        finally:
            app.dependency_overrides.pop(catalog_context, None)
            app.dependency_overrides.pop(get_session, None)
