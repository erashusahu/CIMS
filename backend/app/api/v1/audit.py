from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.db.base import get_session
from app.models.entities import AuditLog

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
async def list_audit_logs(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    query = select(AuditLog).where(AuditLog.business_id == principal.business.id)
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.business_id == principal.business.id))
    records = (await session.execute(query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [{"id": r.id, "actor_user_id": r.actor_user_id, "timestamp": r.created_at, "action": r.action, "affected_type": r.affected_type, "affected_id": r.affected_id, "request_id": r.request_id, "changes": r.changes} for r in records], "total": total, "page": page, "page_size": page_size}
