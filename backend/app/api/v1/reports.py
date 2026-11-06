"""Permission-gated operational reporting for Stage 3J."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError
from app.db.base import get_session
from app.schemas.reports import (
    AgingPage,
    OperationsOverview,
    OrderTaxSupportRow,
    PurchaseSummaryRow,
    SalesSummaryRow,
)
from app.services.reports import ReportsService

router = APIRouter(prefix="/reports", tags=["reports"])


async def reports_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> ReportsService:
    service = ReportsService(session, context)
    await service.prepare()
    return service


@router.get("/overview", response_model=OperationsOverview)
async def overview(service: ReportsService = Depends(reports_service)):
    return await service.overview()


def check_dates(from_date: date | None, to_date: date | None):
    if from_date and to_date and from_date > to_date:
        raise ApiError(422, "report_date_range", "Start date must be on or before end date")


@router.get("/fifo-aging", response_model=AgingPage)
async def fifo_aging(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    item_id: UUID | None = None,
    location_id: UUID | None = None,
    service: ReportsService = Depends(reports_service),
):
    return await service.aging(page, page_size, item_id, location_id)


@router.get("/sales-summary", response_model=list[SalesSummaryRow])
async def sales_summary(
    from_date: date | None = None,
    to_date: date | None = None,
    service: ReportsService = Depends(reports_service),
):
    check_dates(from_date, to_date)
    return await service.sales_summary(from_date, to_date)


@router.get("/purchase-summary", response_model=list[PurchaseSummaryRow])
async def purchase_summary(
    from_date: date | None = None,
    to_date: date | None = None,
    service: ReportsService = Depends(reports_service),
):
    check_dates(from_date, to_date)
    return await service.purchase_summary(from_date, to_date)


@router.get("/order-tax-support", response_model=list[OrderTaxSupportRow])
async def order_tax_support(
    from_date: date | None = None,
    to_date: date | None = None,
    service: ReportsService = Depends(reports_service),
):
    check_dates(from_date, to_date)
    return await service.order_tax_support(from_date, to_date)
