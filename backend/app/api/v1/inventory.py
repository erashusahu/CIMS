import uuid
from typing import Literal

from app.api.catalog_access import CatalogContext, catalog_context
from app.api.v1.catalog import commit_operation
from app.db.base import get_session
from app.schemas.inventory import (
    DocumentVersion,
    LocationIn,
    LocationOption,
    MovementOut,
    OpeningCsvIn,
    OpeningImportPreview,
    OpeningIn,
    OpeningOut,
    OpeningUpdate,
    StockBalanceOut,
    StockDifferenceOut,
    StockPage,
)
from app.services.inventory import InventoryService
from app.services.opening_import import OpeningImporter, opening_template
from app.services.opening_stock import OpeningStockService
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/inventory", tags=["inventory"])


async def inventory_service(
    context: CatalogContext = Depends(catalog_context), session: AsyncSession = Depends(get_session)
):
    service = InventoryService(session, context)
    await service.prepare()
    return service


async def opening_service(service: InventoryService = Depends(inventory_service)):
    return OpeningStockService(service.session, service.context)


@router.get("/opening-status")
async def opening_status(service: InventoryService = Depends(inventory_service)):
    row = (
        await service.session.execute(
            text(
                "SELECT to_regclass('cims.opening_stock') IS NOT NULL "
                "AND to_regclass('cims.opening_stock_line') IS NOT NULL "
                "AND to_regclass('cims.outbox_event') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                "JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='cims' "
                "AND t.typname='document_kind_t' AND e.enumlabel='OPENING_STOCK') AS ready"
            )
        )
    ).one()
    return {
        "ready": row.ready,
        "required_sql": "003_cims_opening_stock.sql",
        "can_write": service.context.can_write,
    }


@router.get("/locations", response_model=list[LocationOption])
async def locations(service: OpeningStockService = Depends(opening_service)):
    return await service.locations()


@router.post("/locations", response_model=LocationOption, status_code=201)
async def create_location(
    payload: LocationIn, service: OpeningStockService = Depends(opening_service)
):
    return await commit_operation(service.session, service.create_location(payload))


@router.get("/opening-stock", response_model=StockPage[OpeningOut])
async def opening_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: OpeningStockService = Depends(opening_service),
):
    return await service.list_documents(page, page_size)


@router.get("/opening-stock/{document_id}", response_model=OpeningOut)
async def opening_document(
    document_id: uuid.UUID, service: OpeningStockService = Depends(opening_service)
):
    return await service.document(document_id)


@router.post("/opening-stock", response_model=OpeningOut, status_code=201)
async def create_opening_document(
    payload: OpeningIn, service: OpeningStockService = Depends(opening_service)
):
    return await commit_operation(service.session, service.save(payload))


@router.patch("/opening-stock/{document_id}", response_model=OpeningOut)
async def update_opening_document(
    document_id: uuid.UUID,
    payload: OpeningUpdate,
    service: OpeningStockService = Depends(opening_service),
):
    return await commit_operation(service.session, service.save(payload, document_id))


@router.post("/opening-stock/{document_id}/post", response_model=OpeningOut)
async def post_opening_document(
    document_id: uuid.UUID,
    payload: DocumentVersion,
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    ),
    service: OpeningStockService = Depends(opening_service),
):
    return await commit_operation(
        service.session, service.post(document_id, payload.expected_version, idempotency_key)
    )


@router.post("/opening-stock/{document_id}/cancel", response_model=OpeningOut)
async def cancel_opening_document(
    document_id: uuid.UUID,
    payload: DocumentVersion,
    service: OpeningStockService = Depends(opening_service),
):
    return await commit_operation(
        service.session, service.cancel(document_id, payload.expected_version)
    )


@router.get("/opening-stock/imports/template", response_class=PlainTextResponse)
async def opening_import_template(service: OpeningStockService = Depends(opening_service)):
    return PlainTextResponse(
        opening_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cims-opening-stock.csv"'},
    )


@router.post("/opening-stock/imports/validate", response_model=OpeningImportPreview)
async def validate_opening_import(
    payload: OpeningCsvIn, service: OpeningStockService = Depends(opening_service)
):
    preview, _ = await OpeningImporter(service).validate(payload.csv_text, payload.business_date)
    return preview


@router.post("/opening-stock/imports/create-draft", response_model=OpeningOut, status_code=201)
async def create_opening_import(
    payload: OpeningCsvIn, service: OpeningStockService = Depends(opening_service)
):
    return await commit_operation(
        service.session,
        OpeningImporter(service).create_draft(payload.csv_text, payload.business_date),
    )


@router.get("/balances", response_model=StockPage[StockBalanceOut])
async def balances(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    service: InventoryService = Depends(inventory_service),
):
    return await service.balances(page, page_size, item_id, location_id)


@router.get("/movements", response_model=StockPage[MovementOut])
async def movements(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    item_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    stock_state: Literal["PHYSICAL", "ACCOUNTING", "COMMITTED", "INCOMING"] | None = None,
    service: InventoryService = Depends(inventory_service),
):
    return await service.movements(page, page_size, item_id, location_id, stock_state)


@router.get("/reconciliation", response_model=StockPage[StockDifferenceOut])
async def reconciliation(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: InventoryService = Depends(inventory_service),
):
    return await service.reconcile(page, page_size)
