import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, current_principal
from app.core.config import settings
from app.core.errors import ApiError
from app.core.security import create_access_token, hash_password, hash_refresh_token, make_refresh_token, verify_password
from app.db.base import get_session
from app.db.context import set_tenant_context
from app.models.entities import AuthSession, Business, Role, User
from app.schemas.foundation import AuthOut, ChangePasswordIn, IdentityOut, LoginIn, RegistrationIn
from app.services.audit import record_audit

router = APIRouter(prefix="/auth", tags=["auth"])


def validate_origin(request: Request, *, required: bool = False) -> None:
    origin = request.headers.get("origin")
    if (required and not origin) or (origin and not hmac.compare_digest(origin.rstrip("/"), settings.frontend_origin.rstrip("/"))):
        raise ApiError(403, "origin_not_allowed", "Request origin is not allowed")


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(settings.refresh_cookie, token, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/api/v1/auth", max_age=settings.session_days * 86400)


async def throttle_attempt(session: AsyncSession, scope: str, key_hash: str, limit: int, window: timedelta, now: datetime) -> bool:
    result = await session.execute(text("SELECT id,attempts,window_started_at,blocked_until FROM auth_throttles WHERE scope=:scope AND key_hash=:key FOR UPDATE"), {"scope": scope, "key": key_hash})
    row = result.first()
    if row and row.blocked_until and row.blocked_until > now:
        return True
    if row and row.window_started_at > now - window:
        attempts = row.attempts + 1
        started = row.window_started_at
        blocked = now + window if attempts >= limit else None
        await session.execute(text("UPDATE auth_throttles SET attempts=:attempts,window_started_at=:started,blocked_until=:blocked WHERE id=:id"), {"attempts": attempts, "started": started, "blocked": blocked, "id": row.id})
        return attempts > limit
    if row:
        await session.execute(text("UPDATE auth_throttles SET attempts=1,window_started_at=:now,blocked_until=NULL WHERE id=:id"), {"now": now, "id": row.id})
    else:
        await session.execute(text("INSERT INTO auth_throttles(scope,key_hash,attempts,window_started_at) VALUES(:scope,:key,1,:now)"), {"scope": scope, "key": key_hash, "now": now})
    return False


@router.post("/register", status_code=201)
async def register(payload: RegistrationIn, request: Request, session: AsyncSession = Depends(get_session)):
    validate_origin(request)
    ip = request.client.host if request.client else "unknown"
    now = datetime.now(UTC)
    key_hash = hashlib.sha256(ip.encode()).hexdigest()
    if await throttle_attempt(session, "register", key_hash, settings.registration_attempt_limit, timedelta(hours=1), now):
        await session.commit()
        raise ApiError(429, "registration_throttled", "Too many registration attempts; try again later")
    business_id, user_id = uuid.uuid4(), uuid.uuid4()
    try:
        await session.execute(text("SELECT cims_register_business(:bid,:identifier,:name,:profile,:uid,:owner,:email,:normalized,:password_hash)"), {
            "bid": business_id, "identifier": payload.business_identifier, "name": payload.business_name,
            "profile": payload.profile, "uid": user_id, "owner": payload.owner_name,
            "email": str(payload.email), "normalized": str(payload.email).lower(), "password_hash": hash_password(payload.password),
        })
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23505":
            raise ApiError(409, "business_identifier_conflict", "That business identifier or owner email is already registered") from None
        raise
    return {"message": "Business registered successfully", "business_identifier": payload.business_identifier}


async def create_session(session: AsyncSession, user: User) -> tuple[AuthSession, str]:
    refresh = make_refresh_token()
    auth_session = AuthSession(business_id=user.business_id, user_id=user.id, refresh_token_hash=hash_refresh_token(refresh), family_id=uuid.uuid4(), expires_at=datetime.now(UTC) + timedelta(days=settings.session_days))
    session.add(auth_session)
    await session.flush()
    await session.execute(text("INSERT INTO auth_refresh_tokens(id,session_id,token_hash) VALUES(:id,:sid,:hash)"), {"id": uuid.uuid4(), "sid": auth_session.id, "hash": hash_refresh_token(refresh)})
    return auth_session, refresh


@router.post("/login", response_model=AuthOut)
async def login(payload: LoginIn, request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    validate_origin(request)
    ip = request.client.host if request.client else "unknown"
    throttle_key = hashlib.sha256(f"{payload.business_identifier}|{str(payload.email).lower()}|{ip}".encode()).hexdigest()
    now = datetime.now(UTC)
    found = await session.execute(text("SELECT id,attempts,window_started_at,blocked_until FROM auth_throttles WHERE scope='login' AND key_hash=:key FOR UPDATE"), {"key": throttle_key})
    throttle = found.first()
    if throttle and throttle.blocked_until and throttle.blocked_until > now:
        await session.rollback()
        raise ApiError(429, "login_throttled", "Too many failed sign-in attempts; try again later")
    lookup = await session.execute(text("SELECT * FROM cims_auth_lookup(:identifier,:email)"), {"identifier": payload.business_identifier, "email": str(payload.email).lower()})
    identity = lookup.first()
    valid = identity is not None and not identity.business_suspended and not identity.user_suspended and verify_password(payload.password, identity.password_hash)
    if identity and identity.password_change_required and identity.temporary_password_expires_at and identity.temporary_password_expires_at <= now:
        valid = False
    if not valid:
        attempts = throttle.attempts + 1 if throttle and throttle.window_started_at > now - timedelta(minutes=15) else 1
        blocked_until = now + timedelta(minutes=15) if attempts >= settings.login_attempt_limit else None
        if throttle:
            await session.execute(text("UPDATE auth_throttles SET attempts=:attempts,window_started_at=:now,blocked_until=:blocked WHERE id=:id"), {"attempts": attempts, "now": now, "blocked": blocked_until, "id": throttle.id})
        else:
            await session.execute(text("INSERT INTO auth_throttles(scope,key_hash,attempts,window_started_at,blocked_until) VALUES('login',:key,:attempts,:now,:blocked)"), {"key": throttle_key, "attempts": attempts, "now": now, "blocked": blocked_until})
        await session.commit()
        raise ApiError(401, "invalid_credentials", "Business identifier, email, or password is incorrect")
    await session.execute(text("DELETE FROM auth_throttles WHERE scope='login' AND key_hash=:key"), {"key": throttle_key})
    business_id, user_id = identity.business_id, identity.user_id
    await set_tenant_context(session, business_id, user_id, request.state.request_id)
    user = await session.get(User, user_id)
    auth_session, refresh = await create_session(session, user)
    await session.commit()
    set_refresh_cookie(response, refresh)
    return AuthOut(access_token=create_access_token(user_id, business_id, auth_session.id), expires_in=settings.access_token_minutes * 60, password_change_required=identity.password_change_required)


@router.post("/refresh", response_model=AuthOut)
async def refresh(request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    validate_origin(request, required=True)
    raw = request.cookies.get(settings.refresh_cookie)
    if not raw:
        raise ApiError(401, "invalid_session", "Your session has expired. Please sign in again")
    token_hash = hash_refresh_token(raw)
    result = await session.execute(text("SELECT t.session_id,s.business_id,s.user_id,s.family_id,s.expires_at,s.revoked_at,t.used_at FROM auth_refresh_tokens t JOIN auth_sessions s ON s.id=t.session_id WHERE t.token_hash=:hash FOR UPDATE OF s,t"), {"hash": token_hash})
    old = result.first()
    now = datetime.now(UTC)
    if not old:
        await session.rollback()
        raise ApiError(401, "invalid_session", "Your session has expired. Please sign in again")
    if old.used_at:
        await session.execute(text("UPDATE auth_sessions SET revoked_at=:now WHERE family_id=:family AND revoked_at IS NULL"), {"now": now, "family": old.family_id})
        await session.commit()
        raise ApiError(401, "refresh_reused", "Session reuse detected; sign in again")
    if old.revoked_at:
        await session.execute(text("UPDATE auth_sessions SET revoked_at=:now WHERE family_id=:family AND revoked_at IS NULL"), {"now": now, "family": old.family_id})
        await session.commit()
        raise ApiError(401, "refresh_reused", "Session reuse detected; sign in again")
    if old.expires_at <= now:
        await session.execute(update(AuthSession).where(AuthSession.id == old.session_id).values(revoked_at=now))
        await session.commit()
        raise ApiError(401, "invalid_session", "Your session has expired. Please sign in again")
    await set_tenant_context(session, old.business_id, old.user_id, request.state.request_id)
    user = await session.get(User, old.user_id)
    business = await session.get(Business, old.business_id)
    if not user or not business or user.suspended or business.suspended:
        await session.rollback()
        raise ApiError(401, "invalid_session", "Your session is no longer active")
    new_token = make_refresh_token()
    await session.execute(text("UPDATE auth_refresh_tokens SET used_at=:now WHERE token_hash=:hash"), {"now": now, "hash": token_hash})
    await session.execute(text("INSERT INTO auth_refresh_tokens(id,session_id,token_hash) VALUES(:id,:sid,:hash)"), {"id": uuid.uuid4(), "sid": old.session_id, "hash": hash_refresh_token(new_token)})
    await session.execute(update(AuthSession).where(AuthSession.id == old.session_id).values(refresh_token_hash=hash_refresh_token(new_token), last_rotated_at=now))
    await session.commit()
    set_refresh_cookie(response, new_token)
    return AuthOut(access_token=create_access_token(user.id, business.id, old.session_id), expires_in=settings.access_token_minutes * 60, password_change_required=user.password_change_required)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
    validate_origin(request, required=True)
    now = datetime.now(UTC)
    await session.execute(update(AuthSession).where(AuthSession.id == principal.session.id).values(revoked_at=now))
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="auth.logout", affected_type="user", affected_id=str(principal.user.id), request_id=request.state.request_id)
    await session.commit()
    response.delete_cookie(settings.refresh_cookie, path="/api/v1/auth", secure=settings.cookie_secure, httponly=True, samesite="lax")


@router.post("/change-password", status_code=204)
async def change_password(payload: ChangePasswordIn, request: Request, response: Response, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
    validate_origin(request, required=True)
    if not principal.user.password_change_required:
        raise ApiError(400, "password_change_not_required", "Password change is not required")
    principal.user.password_hash = hash_password(payload.new_password)
    principal.user.password_change_required = False
    principal.user.temporary_password_expires_at = None
    await session.execute(text("UPDATE auth_sessions SET revoked_at=now() WHERE user_id=:uid AND revoked_at IS NULL"), {"uid": principal.user.id})
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="staff.password_changed", affected_type="user", affected_id=str(principal.user.id), request_id=request.state.request_id)
    await session.commit()
    response.delete_cookie(settings.refresh_cookie, path="/api/v1/auth", secure=settings.cookie_secure, httponly=True, samesite="lax")


@router.get("/me", response_model=IdentityOut)
async def me(principal: Principal = Depends(current_principal)):
    return IdentityOut(id=principal.user.id, full_name=principal.user.full_name, email=principal.user.email, role=principal.role, all_locations=principal.user.all_locations, password_change_required=principal.user.password_change_required, business_id=principal.business.id, business_name=principal.business.name, business_identifier=principal.business.identifier)
