import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context, require_local_catalog
from app.core.config import settings
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.models import catalog as db
from app.schemas.catalog import (
    CatalogOptions,
    CatalogPage,
    ConvertIn,
    ConvertOut,
    CsvIn,
    GroupIn,
    GroupOut,
    ImportPreview,
    ItemIn,
    ItemOut,
    ItemSummaryOut,
    ItemTypeIn,
    ItemTypeOut,
    ItemUpdate,
    UnitIn,
    UnitOut,
    WorkspaceIn,
    WorkspaceOut,
)
from app.services.catalog import CatalogService
from app.services.catalog_rules import csv_template

router = APIRouter(prefix="/catalog", tags=["catalog"])


async def catalog_service(
    context: CatalogContext = Depends(catalog_context), session: AsyncSession = Depends(get_session)
) -> CatalogService:
    service = CatalogService(session, context)
    await service.prepare()
    return service


async def commit_operation(session: AsyncSession, operation):
    try:
        result = await operation
        await session.commit()
        return result
    except IntegrityError as exc:
        await session.rollback()
        if count_freeze_error(exc):
            raise ApiError(
                409, "count_scope_frozen", "Stock is frozen for an active count"
            ) from None
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise ApiError(
                409,
                "catalog_identifier_conflict",
                "That code, SKU or barcode already exists in this business",
            ) from None
        if sqlstate in {"23503", "23514", "23502"}:
            raise ApiError(
                422,
                "invalid_catalog_record",
                "Check the item's references, units and required values",
            ) from None
        raise
    except Exception:
        await session.rollback()
        raise


@router.get("/local-status")
async def local_status(request: Request, session: AsyncSession = Depends(get_session)):
    require_local_catalog(request)
    columns = set(
        (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='cims' AND table_name='item'"
                )
            )
        ).scalars()
    )
    configured = make_url(settings.database_url)
    return {
        "local_mode": True,
        "ready": {"catalog_version", "cost_center_id"}.issubset(columns),
        "required_sql": "002_cims_catalog_extension.sql",
        "isolated_test_database": configured.host == "127.0.0.1"
        and configured.port == 55439
        and configured.username == "cims_test_admin",
    }


@router.get("/local-workspaces", response_model=list[WorkspaceOut])
async def local_workspaces(request: Request, session: AsyncSession = Depends(get_session)):
    require_local_catalog(request)
    rows = await session.execute(
        select(db.tenants)
        .where(db.tenants.c.status.in_(["ACTIVE", "TRIAL"]))
        .order_by(db.tenants.c.name)
    )
    return [dict(row) for row in rows.mappings()]


@router.post("/local-workspaces", response_model=WorkspaceOut, status_code=201)
async def create_local_workspace(
    payload: WorkspaceIn, request: Request, session: AsyncSession = Depends(get_session)
):
    require_local_catalog(request)

    async def create():
        row = (
            (
                await session.execute(
                    insert(db.tenants)
                    .values(
                        tenant_id=uuid.uuid4(),
                        name=payload.name,
                        slug=payload.slug,
                        status="ACTIVE",
                        default_valuation_method="WEIGHTED_AVERAGE",
                    )
                    .returning(db.tenants)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    return await commit_operation(session, create())


@router.get("/options", response_model=CatalogOptions)
async def options(service: CatalogService = Depends(catalog_service)):
    return await service.options()


@router.get("/items", response_model=CatalogPage[ItemSummaryOut])
async def list_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=100),
    status: Literal["ACTIVE", "INACTIVE", "DRAFT"] | None = None,
    service: CatalogService = Depends(catalog_service),
):
    return await service.page(db.items, "item_id", page, page_size, search=search, status=status)


@router.get("/items/{item_id}", response_model=ItemOut)
async def get_item(item_id: uuid.UUID, service: CatalogService = Depends(catalog_service)):
    return await service.item_details(item_id)


@router.post("/items", response_model=ItemOut, status_code=201)
async def create_item(payload: ItemIn, service: CatalogService = Depends(catalog_service)):
    return await commit_operation(service.session, service.save_item(payload))


@router.patch("/items/{item_id}", response_model=ItemOut)
async def update_item(
    item_id: uuid.UUID, payload: ItemUpdate, service: CatalogService = Depends(catalog_service)
):
    return await commit_operation(service.session, service.save_item(payload, item_id))


@router.get("/units", response_model=CatalogPage[UnitOut])
async def list_units(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: CatalogService = Depends(catalog_service),
):
    return await service.page(db.units, "uom_id", page, page_size)


@router.post("/units", response_model=UnitOut, status_code=201)
async def create_unit(payload: UnitIn, service: CatalogService = Depends(catalog_service)):
    return await commit_operation(service.session, service.create_unit(payload))


@router.patch("/units/{unit_id}", response_model=UnitOut)
async def update_unit(
    unit_id: uuid.UUID, payload: UnitIn, service: CatalogService = Depends(catalog_service)
):
    return await commit_operation(service.session, service.update_unit(unit_id, payload))


@router.get("/groups", response_model=CatalogPage[GroupOut])
async def list_groups(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: CatalogService = Depends(catalog_service),
):
    result = await service.page(db.groups, "item_group_id", page, page_size)
    for group in result["items"]:
        group["attributes"] = await service.group_names(group["item_group_id"])
    return result


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(payload: GroupIn, service: CatalogService = Depends(catalog_service)):
    return await commit_operation(service.session, service.create_group(payload))


@router.get("/item-types", response_model=CatalogPage[ItemTypeOut])
async def list_types(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: CatalogService = Depends(catalog_service),
):
    return await service.page(db.item_types, "item_type_id", page, page_size)


@router.post("/item-types", response_model=ItemTypeOut, status_code=201)
async def create_type(payload: ItemTypeIn, service: CatalogService = Depends(catalog_service)):
    return await commit_operation(service.session, service.create_type(payload))


@router.post("/convert", response_model=ConvertOut)
async def convert(payload: ConvertIn, service: CatalogService = Depends(catalog_service)):
    return await service.convert(payload.item_id, payload.from_uom_id, payload.quantity)


@router.get("/imports/template", response_class=PlainTextResponse)
async def import_template(service: CatalogService = Depends(catalog_service)):
    return PlainTextResponse(
        csv_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cims-items-template.csv"'},
    )


@router.post("/imports/validate", response_model=ImportPreview)
async def validate_csv(payload: CsvIn, service: CatalogService = Depends(catalog_service)):
    preview, _ = await service.validate_csv(payload.csv_text)
    return preview


@router.post("/imports/commit", status_code=201)
async def commit_csv(payload: CsvIn, service: CatalogService = Depends(catalog_service)):
    return await commit_operation(service.session, service.commit_csv(payload.csv_text))
