from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, require_admin
from app.core.errors import ApiError
from app.db.base import get_session
from app.models.entities import Business, Role
from app.schemas.foundation import BusinessOut, BusinessPatch
from app.services.audit import record_audit

router = APIRouter(tags=["business"])


@router.get("/business", response_model=BusinessOut)
async def get_business(principal: Principal = Depends(require_admin)):
    return principal.business


@router.patch("/business", response_model=BusinessOut)
async def update_business(payload: BusinessPatch, request: Request, principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    previous = principal.business.name
    principal.business.name = payload.name
    await record_audit(session, business_id=principal.business.id, actor_user_id=principal.user.id, action="business.name_updated", affected_type="business", affected_id=str(principal.business.id), request_id=request.state.request_id, changes={"before": {"name": previous}, "after": {"name": payload.name}})
    await session.commit()
    await session.refresh(principal.business)
    return principal.business


@router.get("/roles")
async def list_roles(principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Role.name).where(Role.business_id == principal.business.id).order_by(Role.name))
    return {"items": result.scalars().all()}
