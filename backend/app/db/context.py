from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_tenant_context(session: AsyncSession, business_id: UUID, user_id: UUID, request_id: str) -> None:
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true), set_config('app.user_id', :user, true), set_config('app.request_id', :request, true)"),
        {"tenant": str(business_id), "user": str(user_id), "request": request_id},
    )
