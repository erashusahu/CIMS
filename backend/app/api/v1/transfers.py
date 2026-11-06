"""Location transfers, including in-transit stock and variance settlement."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.schemas.transfers import (
    InTransitOut,
    LocationRegistrationIn,
    RegistrationIn,
    RegistrationOut,
    TransferAction,
    TransferIn,
    TransferLocationOut,
    TransferLossAction,
    TransferOut,
    TransferQuantityAction,
)
from app.services.transfers import TransferService

router = APIRouter(prefix="/transfers", tags=["transfers"])


async def transfer_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> TransferService:
    service = TransferService(session, context)
    await service.prepare()
    return service


async def commit(session, operation):
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
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise ApiError(
                409, "transfer_conflict", "This transfer conflicts with an existing record"
            ) from None
        if code in {"23503", "23514", "23502"}:
            raise ApiError(
                422, "invalid_transfer", "Review the transfer details and stock state"
            ) from None
        raise
    except Exception:
        await session.rollback()
        raise


@router.get("/status")
async def status(service: TransferService = Depends(transfer_service)):
    ready = await service.session.scalar(
        text("SELECT to_regclass('cims.transfer_operation') IS NOT NULL")
    )
    return {"ready": bool(ready), "required_sql": "007_cims_transfer_extension.sql"}


@router.get("/registrations", response_model=list[RegistrationOut])
async def registrations(service: TransferService = Depends(transfer_service)):
    return await service.registrations()


@router.post("/registrations", response_model=RegistrationOut, status_code=201)
async def create_registration(
    payload: RegistrationIn, service: TransferService = Depends(transfer_service)
):
    return await commit(service.session, service.create_registration(payload))


@router.get("/locations", response_model=list[TransferLocationOut])
async def locations(service: TransferService = Depends(transfer_service)):
    return await service.locations()


@router.post("/locations/{location_id}/registration", response_model=TransferLocationOut)
async def assign_registration(
    location_id: uuid.UUID,
    payload: LocationRegistrationIn,
    service: TransferService = Depends(transfer_service),
):
    return await commit(
        service.session, service.assign_registration(location_id, payload.tax_registration_id)
    )


@router.get("", response_model=list[TransferOut])
async def list_transfers(service: TransferService = Depends(transfer_service)):
    return await service.list()


@router.get("/in-transit", response_model=list[InTransitOut])
async def in_transit(service: TransferService = Depends(transfer_service)):
    return await service.in_transit()


@router.post("", response_model=TransferOut, status_code=201)
async def create_transfer(
    payload: TransferIn, service: TransferService = Depends(transfer_service)
):
    return await commit(service.session, service.create(payload))


@router.get("/{order_id}", response_model=TransferOut)
async def transfer(order_id: uuid.UUID, service: TransferService = Depends(transfer_service)):
    return await service.document(order_id)


@router.post("/{order_id}/confirm", response_model=TransferOut)
async def confirm(
    order_id: uuid.UUID,
    payload: TransferAction,
    service: TransferService = Depends(transfer_service),
):
    return await commit(service.session, service.confirm(order_id, payload.idempotency_key))


@router.post("/{order_id}/dispatch", response_model=TransferOut)
async def dispatch(
    order_id: uuid.UUID,
    payload: TransferAction,
    service: TransferService = Depends(transfer_service),
):
    return await commit(service.session, service.dispatch(order_id, payload.idempotency_key))


@router.post("/{order_id}/receive", response_model=TransferOut)
async def receive(
    order_id: uuid.UUID,
    payload: TransferQuantityAction,
    service: TransferService = Depends(transfer_service),
):
    return await commit(service.session, service.resolve(order_id, payload))


@router.post("/{order_id}/settle-loss", response_model=TransferOut)
async def settle_loss(
    order_id: uuid.UUID,
    payload: TransferLossAction,
    service: TransferService = Depends(transfer_service),
):
    return await commit(service.session, service.resolve(order_id, payload, loss=True))
