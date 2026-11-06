"""POS draft setup. No checkout, invoice or payment is posted here."""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError
from app.db.base import get_session
from app.schemas.customer import CustomerOut
from app.schemas.sales import SalesOrderOut
from app.services.pos import PosService

router = APIRouter(prefix="/pos", tags=["pos"])


class PosDiscardIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


async def pos_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> PosService:
    service = PosService(session, context)
    await service.sales.catalog.prepare()
    service.sales.authorize_read()
    return service


@router.get("/status")
async def status(service: PosService = Depends(pos_service)):
    ready = await service.session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='cims' AND table_name='sales_order' "
            "AND column_name='sales_source')"
        )
    )
    return {
        "ready": bool(ready),
        "checkout_ready": False,
        "required_sql": "009_cims_pos_draft_source.sql",
    }


@router.post("/walk-in-customer", response_model=CustomerOut)
async def walk_in_customer(
    location_id: uuid.UUID,
    service: PosService = Depends(pos_service),
):
    try:
        result = await service.walk_in_customer(location_id)
        await service.session.commit()
        return result
    except IntegrityError as exc:
        await service.session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise ApiError(
                409, "walk_in_customer_conflict", "Reload the walk-in customer"
            ) from None
        raise
    except Exception:
        await service.session.rollback()
        raise


@router.post("/carts/{order_id}/discard", response_model=SalesOrderOut)
async def discard_cart(
    order_id: uuid.UUID,
    payload: PosDiscardIn,
    service: PosService = Depends(pos_service),
):
    try:
        result = await service.discard(order_id, payload.expected_revision)
        await service.session.commit()
        return result
    except Exception:
        await service.session.rollback()
        raise
