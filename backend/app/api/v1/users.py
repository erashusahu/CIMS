import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin, require_password_ready
from app.core.errors import ApiError
from app.core.security import hash_password
from app.db.base import get_session
from app.models.entities import AuthSession, Location, Role, User, UserLocationAccess
from app.schemas.foundation import StaffCreatedOut, UserCreateIn, UserPatchIn
from app.services.audit import record_audit

router = APIRouter(prefix="/users", tags=["users"])
ROLE_NAMES = {"Owner", "Admin", "Warehouse Manager", "Warehouse Staff", "Sales", "Procurement", "Accountant", "Viewer"}


def temporary_password() -> str:
    # URL-safe output with sufficient entropy; returned once by the create/reset API only.
    return secrets.token_urlsafe(20)


async def validate_role_and_scope(session: AsyncSession, principal: Principal, role_name: str, all_locations: bool, location_ids: list[uuid.UUID]) -> Role:
    if role_name not in ROLE_NAMES:
        raise ApiError(422, "invalid_role", "Choose one of the fixed business roles")
    if role_name == "Owner":
        raise ApiError(403, "owner_role_reserved", "Owner access cannot be assigned to staff")
    if role_name == "Admin" and principal.role != "Owner":
        raise ApiError(403, "admin_grant_forbidden", "Only the Owner can grant Admin access")
    if not all_locations and not location_ids:
        raise ApiError(422, "location_scope_required", "Select at least one active location")
    role = (await session.execute(select(Role).where(Role.business_id == principal.business.id, Role.name == role_name))).scalar_one_or_none()
    if role is None:
        raise ApiError(422, "invalid_role", "Role is unavailable")
    if not all_locations:
        locations = (await session.execute(select(Location.id).where(Location.business_id == principal.business.id, Location.id.in_(location_ids), Location.active.is_(True)))).scalars().all()
        if set(locations) != set(location_ids):
            raise ApiError(422, "invalid_location_scope", "Every selected location must be active and belong to this business")
    return role


async def staff_payload(session: AsyncSession, user: User) -> dict:
    role = await session.get(Role, user.role_id)
    scopes = (await session.execute(select(UserLocationAccess.location_id).where(UserLocationAccess.user_id == user.id))).scalars().all()
    return {"id": user.id, "full_name": user.full_name, "email": user.email, "role": role.name, "all_locations": user.all_locations, "location_ids": scopes, "suspended": user.suspended, "password_change_required": user.password_change_required}


@router.get("")
async def list_users(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    base = select(User).where(User.business_id == principal.business.id, User.id != principal.user.id)
    count = await session.scalar(select(func.count()).select_from(User).where(User.business_id == principal.business.id, User.id != principal.user.id))
    users = (await session.execute(base.order_by(User.full_name).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = [await staff_payload(session, user) for user in users]
    return {"items": items, "total": count, "page": page, "page_size": page_size}


@router.post("", response_model=StaffCreatedOut, status_code=201)
async def create_user(payload: UserCreateIn, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    role = await validate_role_and_scope(session, principal, payload.role, payload.all_locations, payload.location_ids)
    temp = temporary_password()
    user = User(business_id=principal.business.id, full_name=payload.full_name, email=str(payload.email), email_normalized=str(payload.email).lower(), password_hash=hash_password(temp), role_id=role.id, all_locations=payload.all_locations, password_change_required=True, temporary_password_expires_at=datetime.now(UTC) + timedelta(hours=24))
    session.add(user)
    await session.flush()
    if not payload.all_locations:
        session.add_all([UserLocationAccess(business_id=principal.business.id, user_id=user.id, location_id=location_id) for location_id in payload.location_ids])
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="staff.created", affected_type="user", affected_id=str(user.id), request_id=request.state.request_id, changes={"after": {"email": user.email_normalized, "role": role.name, "all_locations": user.all_locations, "location_ids": [str(value) for value in payload.location_ids]}})
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "staff_email_conflict", "An account with this email already exists in this business") from None
    result = await staff_payload(session, user)
    result["temporary_password"] = temp
    return result


@router.patch("/{user_id}")
async def update_user(user_id: uuid.UUID, payload: UserPatchIn, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    user = (await session.execute(select(User).where(User.id == user_id, User.business_id == principal.business.id).with_for_update())).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "user_not_found", "Staff account was not found")
    target_role = (await session.execute(select(Role).where(Role.id == user.role_id))).scalar_one()
    if user.id == principal.user.id or target_role.name in {"Owner", "Admin"}:
        raise ApiError(403, "staff_account_protected", "This account cannot be changed by this administrator")
    before = await staff_payload(session, user)
    data = payload.model_dump(exclude_unset=True)
    new_role_name = data.get("role", target_role.name)
    new_all = data.get("all_locations", user.all_locations)
    new_ids = data.get("location_ids")
    if new_ids is None:
        new_ids = [] if new_all else (await session.execute(select(UserLocationAccess.location_id).where(UserLocationAccess.user_id == user.id))).scalars().all()
    if "role" in data or "all_locations" in data or "location_ids" in data:
        role = await validate_role_and_scope(session, principal, new_role_name, new_all, new_ids)
        user.role_id = role.id
        user.all_locations = new_all
        await session.execute(text("DELETE FROM user_location_access WHERE user_id=:uid"), {"uid": user.id})
        if not new_all:
            session.add_all([UserLocationAccess(business_id=principal.business.id, user_id=user.id, location_id=location_id) for location_id in new_ids])
    if "full_name" in data and data["full_name"] is not None:
        user.full_name = data["full_name"]
    status_changed = "suspended" in data and data["suspended"] != user.suspended
    if status_changed:
        user.suspended = data["suspended"]
        if user.suspended:
            await session.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC)))
    after = await staff_payload(session, user)
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="staff.updated", affected_type="user", affected_id=str(user.id), request_id=request.state.request_id, changes={"before": before, "after": after})
    if status_changed:
        await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="staff.suspended" if user.suspended else "staff.reactivated", affected_type="user", affected_id=str(user.id), request_id=request.state.request_id, changes={"suspended": user.suspended})
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(422, "invalid_location_scope", "Location assignments are invalid") from None
    return after


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: uuid.UUID, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    user = (await session.execute(select(User).where(User.id == user_id, User.business_id == principal.business.id).with_for_update())).scalar_one_or_none()
    if user is None:
        raise ApiError(404, "user_not_found", "Staff account was not found")
    role = await session.get(Role, user.role_id)
    if user.id == principal.user.id or role.name in {"Owner", "Admin"}:
        raise ApiError(403, "staff_account_protected", "This account cannot be reset by this administrator")
    temp = temporary_password()
    user.password_hash = hash_password(temp)
    user.password_change_required = True
    user.temporary_password_expires_at = datetime.now(UTC) + timedelta(hours=24)
    await session.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(UTC)))
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="staff.password_reset", affected_type="user", affected_id=str(user.id), request_id=request.state.request_id)
    await session.commit()
    return {"temporary_password": temp, "expires_at": user.temporary_password_expires_at}
