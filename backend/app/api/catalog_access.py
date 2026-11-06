"""Catalog identity, with an explicit loopback-only development workspace mode."""

import ipaddress
from dataclasses import dataclass
from uuid import UUID

from app.api.deps import bearer, current_principal
from app.core.config import settings
from app.core.errors import ApiError
from app.db.base import get_session
from app.models.catalog import tenants
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class CatalogContext:
    tenant_id: UUID
    user_id: UUID | None
    role: str
    request_id: str
    local: bool = False

    @property
    def can_write(self) -> bool:
        return self.local or self.role in {"Owner", "Admin"}


def is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def require_local_catalog(request: Request) -> None:
    if not settings.catalog_local_mode or settings.app_env != "development":
        raise ApiError(404, "not_found", "This workspace is unavailable")
    if (
        not request.client
        or not is_loopback(request.client.host)
        or not is_loopback(request.url.hostname)
    ):
        raise ApiError(
            403, "local_workspace_only", "This workspace is available only on this computer"
        )
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.frontend_origin.rstrip("/"):
        raise ApiError(403, "origin_not_allowed", "Request origin is not allowed")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not origin:
        raise ApiError(403, "origin_required", "A local workspace origin is required")


async def catalog_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> CatalogContext:
    if settings.catalog_local_mode:
        require_local_catalog(request)
        try:
            tenant_id = UUID(request.headers.get("X-CIMS-Tenant-ID", ""))
        except ValueError:
            raise ApiError(422, "workspace_required", "Select a business workspace first") from None
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
        if not await session.scalar(
            select(tenants.c.tenant_id).where(
                tenants.c.tenant_id == tenant_id, tenants.c.status.in_(["ACTIVE", "TRIAL"])
            )
        ):
            raise ApiError(404, "workspace_not_found", "The selected workspace is unavailable")
        return CatalogContext(tenant_id, None, "Local", request.state.request_id, local=True)
    principal = await current_principal(request, credentials, session)
    if principal.user.password_change_required:
        raise ApiError(
            403, "password_change_required", "Change your temporary password before continuing"
        )
    return CatalogContext(
        principal.business.id, principal.user.id, principal.role, request.state.request_id
    )
