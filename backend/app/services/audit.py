import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AuditLog

SECRET_KEYS = {"password", "password_hash", "refresh_token", "access_token", "token", "secret", "credential"}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items() if key.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


async def record_audit(session: AsyncSession, *, business_id: uuid.UUID, actor_user_id: uuid.UUID | None, action: str, affected_type: str, affected_id: str, request_id: str, changes: dict | None = None) -> None:
    session.add(AuditLog(business_id=business_id, actor_user_id=actor_user_id, action=action, affected_type=affected_type, affected_id=affected_id, request_id=request_id, changes=sanitize(changes or {})))
