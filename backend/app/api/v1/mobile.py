"""Mobile sync reuses domain services and records accepted results atomically."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.schemas.mobile import MobileOperationIn, MobileReceiptOut
from app.services.catalog import CatalogService
from app.services.mobile import MobileService

router = APIRouter(prefix="/mobile", tags=["mobile"])


async def mobile_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> MobileService:
    await CatalogService(session, context).prepare()
    return MobileService(session, context)


@router.get("/status")
async def status(service: MobileService = Depends(mobile_service)):
    ready = await service.session.scalar(
        text("SELECT to_regclass('cims.mobile_operation_receipt') IS NOT NULL")
    )
    return {"ready": bool(ready), "required_sql": "010_cims_mobile_operation_receipts.sql"}


@router.get("/receipts/{operation_id}", response_model=MobileReceiptOut)
async def receipt(operation_id: uuid.UUID, service: MobileService = Depends(mobile_service)):
    return await service.receipt(operation_id)


@router.post("/operations", response_model=MobileReceiptOut)
async def synchronize(payload: MobileOperationIn, service: MobileService = Depends(mobile_service)):
    try:
        result = await service.submit(payload)
        await service.session.commit()
        return result
    except IntegrityError as exc:
        await service.session.rollback()
        if count_freeze_error(exc):
            raise ApiError(
                409, "count_scope_frozen", "Stock is frozen for an active count"
            ) from None
        raise
    except Exception:
        await service.session.rollback()
        raise
