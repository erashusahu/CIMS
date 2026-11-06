import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.security import decode_access_token
from app.db.base import get_session
from app.db.context import set_tenant_context
from app.models.entities import AuthSession, Business, Role, User

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    business: Business
    role: str
    session: AuthSession


async def current_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), session: AsyncSession = Depends(get_session)) -> Principal:
    if credentials is None:
        raise ApiError(401, "authentication_required", "Please sign in")
    try:
        claims = decode_access_token(credentials.credentials)
        user_id, business_id, session_id = uuid.UUID(claims["sub"]), uuid.UUID(claims["tid"]), uuid.UUID(claims["sid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise ApiError(401, "invalid_session", "Your session has expired. Please sign in again") from None
    await set_tenant_context(session, business_id, user_id, request.state.request_id)
    result = await session.execute(
        select(User, Business, Role, AuthSession)
        .join(Business, Business.id == User.business_id)
        .join(Role, Role.id == User.role_id)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(User.id == user_id, User.business_id == business_id, AuthSession.id == session_id)
    )
    row = result.first()
    now = datetime.now(UTC)
    if not row or row.AuthSession.revoked_at or row.AuthSession.expires_at <= now or row.User.suspended or row.Business.suspended:
        raise ApiError(401, "invalid_session", "Your session is no longer active")
    return Principal(row.User, row.Business, row.Role.name, row.AuthSession)


def require_password_ready(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.user.password_change_required:
        raise ApiError(403, "password_change_required", "Change your temporary password before continuing")
    return principal


def require_admin(principal: Principal = Depends(require_password_ready)) -> Principal:
    if principal.role not in {"Owner", "Admin"}:
        raise ApiError(403, "forbidden", "You do not have permission to perform this action")
    return principal


def require_owner(principal: Principal = Depends(require_password_ready)) -> Principal:
    if principal.role != "Owner":
        raise ApiError(403, "forbidden", "Only the business owner can manage Admin access")
    return principal
