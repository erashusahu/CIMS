import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin, require_password_ready
from app.core.errors import ApiError
from app.db.base import get_session
from app.models.entities import Location, UserLocationAccess
from app.schemas.foundation import LocationIn, LocationOut, Page
from app.services.audit import record_audit

router = APIRouter(prefix="/locations", tags=["locations"])


async def location_scope(session: AsyncSession, principal: Principal):
    if principal.role in {"Owner", "Admin"} or principal.user.all_locations:
        return None
    result = await session.execute(select(UserLocationAccess.location_id).where(UserLocationAccess.business_id == principal.business.id, UserLocationAccess.user_id == principal.user.id))
    return result.scalars().all()


@router.get("")
async def list_locations(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), principal: Principal = Depends(require_password_ready), session: AsyncSession = Depends(get_session)):
    scopes = await location_scope(session, principal)
    query = select(Location).where(Location.business_id == principal.business.id)
    count_query = select(func.count()).select_from(Location).where(Location.business_id == principal.business.id)
    if scopes is not None:
        query = query.where(Location.id.in_(scopes), Location.active.is_(True))
        count_query = count_query.where(Location.id.in_(scopes), Location.active.is_(True))
    total = (await session.execute(count_query)).scalar_one()
    rows = (await session.execute(query.order_by(Location.code_normalized).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [LocationOut.model_validate(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{location_id}", response_model=LocationOut)
async def get_location(location_id: uuid.UUID, principal: Principal = Depends(require_password_ready), session: AsyncSession = Depends(get_session)):
    scopes = await location_scope(session, principal)
    query = select(Location).where(Location.id == location_id, Location.business_id == principal.business.id)
    if scopes is not None:
        query = query.where(Location.id.in_(scopes), Location.active.is_(True))
    location = (await session.execute(query)).scalar_one_or_none()
    if location is None:
        raise ApiError(404, "location_not_found", "Location was not found")
    return location


@router.post("", response_model=LocationOut, status_code=201)
async def create_location(payload: LocationIn, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    location = Location(business_id=principal.business.id, **payload.model_dump(exclude={"active"}), code_normalized=payload.code.casefold())
    session.add(location)
    await session.flush()
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="location.created", affected_type="location", affected_id=str(location.id), request_id=request.state.request_id, changes={"after": {"code": location.code, "name": location.name, "type": location.type, "active": location.active}})
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "location_code_conflict", "A location with this code already exists") from None
    await session.refresh(location)
    return location


@router.patch("/{location_id}", response_model=LocationOut)
async def update_location(location_id: uuid.UUID, payload: LocationIn, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    location = (await session.execute(select(Location).where(Location.id == location_id, Location.business_id == principal.business.id))).scalar_one_or_none()
    if location is None:
        raise ApiError(404, "location_not_found", "Location was not found")
    before = {"code": location.code, "name": location.name, "type": location.type, "address": location.address, "city": location.city, "state_code": location.state_code, "pin_code": location.pin_code, "active": location.active}
    for field, value in payload.model_dump().items():
        setattr(location, field, value)
    location.code_normalized = payload.code.casefold()
    after = {"code": location.code, "name": location.name, "type": location.type, "address": location.address, "city": location.city, "state_code": location.state_code, "pin_code": location.pin_code, "active": location.active}
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="location.updated", affected_type="location", affected_id=str(location.id), request_id=request.state.request_id, changes={"before": before, "after": after})
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "location_code_conflict", "A location with this code already exists") from None
    await session.refresh(location)
    return location
