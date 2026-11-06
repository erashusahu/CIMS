"""Phase 3C customer setup endpoints."""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError
from app.db.base import get_session
from app.schemas.customer import (
    AddressIn,
    AddressOut,
    CustomerCsvIn,
    CustomerImportPreview,
    CustomerIn,
    CustomerOut,
    CustomerPage,
    CustomerUpdate,
)
from app.services.customer import CustomerService, customer_template

router = APIRouter(prefix="/customers", tags=["customers"])


async def commit_customer(session, operation):
    try:
        result = await operation
        await session.commit()
        return result
    except IntegrityError as exc:
        await session.rollback()
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise ApiError(
                409, "customer_duplicate", "That customer or address already exists"
            ) from None
        if code in {"23503", "23514", "23502"}:
            raise ApiError(
                422, "invalid_customer_record", "Check the customer and address details"
            ) from None
        raise
    except Exception:
        await session.rollback()
        raise


async def customer_service(
    context: CatalogContext = Depends(catalog_context), session: AsyncSession = Depends(get_session)
):
    service = CustomerService(session, context)
    await service.catalog.prepare()
    return service


@router.get("", response_model=CustomerPage)
async def customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=100),
    service: CustomerService = Depends(customer_service),
):
    return await service.customers(page, page_size, search)


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(
    payload: CustomerIn, service: CustomerService = Depends(customer_service)
):
    return await commit_customer(service.session, service.save(payload))


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    service: CustomerService = Depends(customer_service),
):
    return await commit_customer(service.session, service.save(payload, customer_id))


@router.get("/{customer_id}/addresses", response_model=list[AddressOut])
async def customer_addresses(
    customer_id: uuid.UUID, service: CustomerService = Depends(customer_service)
):
    return await service.addresses(customer_id)


@router.post("/{customer_id}/addresses", response_model=AddressOut, status_code=201)
async def add_customer_address(
    customer_id: uuid.UUID, payload: AddressIn, service: CustomerService = Depends(customer_service)
):
    return await commit_customer(service.session, service.add_address(customer_id, payload))


@router.get("/imports/template", response_class=PlainTextResponse)
async def customer_import_template(_: CustomerService = Depends(customer_service)):
    return PlainTextResponse(
        customer_template(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cims-customers-template.csv"},
    )


@router.post("/imports/validate", response_model=CustomerImportPreview)
async def validate_customer_import(
    payload: CustomerCsvIn, service: CustomerService = Depends(customer_service)
):
    return await service.validate_csv(payload.csv_text)


@router.post("/imports/commit", response_model=CustomerImportPreview, status_code=201)
async def commit_customer_import(
    payload: CustomerCsvIn, service: CustomerService = Depends(customer_service)
):
    return await commit_customer(service.session, service.import_csv(payload.csv_text))
