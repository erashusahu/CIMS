"""Physical returns and frozen stock counts. Financial credits stay external."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.schemas.recovery import (
    CountIn,
    CountOut,
    CountSubmit,
    ReturnAction,
    ReturnIn,
    ReturnOut,
    ShippedOption,
)
from app.services.counts import CountService
from app.services.inventory import InventoryService
from app.services.returns import ReturnService

router = APIRouter(prefix="/recovery", tags=["recovery"])


async def service_context(
    context: CatalogContext = Depends(catalog_context), session: AsyncSession = Depends(get_session)
):
    await InventoryService(session, context).prepare()
    return session, context


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
                409, "recovery_conflict", "This record conflicts with existing data"
            ) from None
        if code in {"23503", "23514", "23502"}:
            raise ApiError(422, "recovery_invalid", "Review the return or count details") from None
        raise
    except Exception:
        await session.rollback()
        raise


@router.get("/status")
async def status(state=Depends(service_context)):
    session, _ = state
    ready = await session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_trigger "
            "WHERE tgrelid='cims.stock_ledger_entry'::regclass "
            "AND tgname='trg_count_freeze' AND NOT tgisinternal)"
        )
    )
    return {"ready": bool(ready), "required_sql": "008_cims_returns_counts_guards.sql"}


@router.get("/returns", response_model=list[ReturnOut])
async def returns(state=Depends(service_context)):
    return await ReturnService(*state).list()


@router.get("/shipped-options", response_model=list[ShippedOption])
async def shipped_options(state=Depends(service_context)):
    return await ReturnService(*state).shipped_options()


@router.post("/returns", response_model=ReturnOut, status_code=201)
async def create_return(payload: ReturnIn, state=Depends(service_context)):
    session, context = state
    return await commit(session, ReturnService(session, context).create(payload))


@router.get("/returns/{return_id}", response_model=ReturnOut)
async def get_return(return_id: uuid.UUID, state=Depends(service_context)):
    return await ReturnService(*state).document(return_id)


@router.post("/returns/{return_id}/approve", response_model=ReturnOut)
async def approve_return(
    return_id: uuid.UUID, payload: ReturnAction, state=Depends(service_context)
):
    session, context = state
    return await commit(
        session, ReturnService(session, context).approve(return_id, payload.idempotency_key)
    )


@router.post("/returns/{return_id}/receive", response_model=ReturnOut)
async def receive_return(
    return_id: uuid.UUID, payload: ReturnAction, state=Depends(service_context)
):
    session, context = state
    return await commit(
        session, ReturnService(session, context).receive(return_id, payload.idempotency_key)
    )


@router.post("/returns/{return_id}/cancel", response_model=ReturnOut)
async def cancel_return(return_id: uuid.UUID, state=Depends(service_context)):
    session, context = state
    return await commit(session, ReturnService(session, context).cancel(return_id))


@router.get("/counts", response_model=list[CountOut])
async def counts(state=Depends(service_context)):
    return await CountService(*state).list()


@router.post("/counts", response_model=CountOut, status_code=201)
async def create_count(payload: CountIn, state=Depends(service_context)):
    session, context = state
    return await commit(session, CountService(session, context).create(payload))


@router.get("/counts/{count_id}", response_model=CountOut)
async def get_count(count_id: uuid.UUID, state=Depends(service_context)):
    return await CountService(*state).document(count_id, hide_blind=True)


@router.post("/counts/{count_id}/start", response_model=CountOut)
async def start_count(count_id: uuid.UUID, state=Depends(service_context)):
    session, context = state
    return await commit(session, CountService(session, context).start(count_id))


@router.post("/counts/{count_id}/submit", response_model=CountOut)
async def submit_count(count_id: uuid.UUID, payload: CountSubmit, state=Depends(service_context)):
    session, context = state
    return await commit(session, CountService(session, context).submit(count_id, payload))


@router.post("/counts/{count_id}/approve", response_model=CountOut)
async def approve_count(count_id: uuid.UUID, state=Depends(service_context)):
    session, context = state
    return await commit(session, CountService(session, context).approve(count_id))


@router.post("/counts/{count_id}/post", response_model=CountOut)
async def post_count(count_id: uuid.UUID, payload: ReturnAction, state=Depends(service_context)):
    session, context = state
    return await commit(
        session, CountService(session, context).post(count_id, payload.idempotency_key)
    )


@router.post("/counts/{count_id}/cancel", response_model=CountOut)
async def cancel_count(count_id: uuid.UUID, state=Depends(service_context)):
    session, context = state
    return await commit(session, CountService(session, context).cancel(count_id))
