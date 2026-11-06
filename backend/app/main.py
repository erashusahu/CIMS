import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.v1 import (
    audit,
    auth,
    business,
    catalog,
    customers,
    inventory,
    locations,
    mobile,
    pos,
    procurement,
    reports,
    recovery,
    sales,
    transfers,
    users,
)
from app.core.config import settings
from app.core.errors import (
    ApiError,
    api_error_handler,
    database_error_handler,
    http_error_handler,
    log_failure,
    unexpected_error_handler,
    validation_error_handler,
)
from app.core.request import RequestContextMiddleware
from app.db.base import engine
from app.db.readiness import check_database_readiness

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="CIMS API",
    version="1.0.0",
    docs_url="/docs" if settings.app_env == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-CIMS-Tenant-ID",
        "Idempotency-Key",
    ],
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(SQLAlchemyError, database_error_handler)
app.add_exception_handler(HTTPException, http_error_handler)
app.add_exception_handler(Exception, unexpected_error_handler)


api = "/api/v1"
app.include_router(auth.router, prefix=api)
app.include_router(business.router, prefix=api)
app.include_router(locations.router, prefix=api)
app.include_router(users.router, prefix=api)
app.include_router(audit.router, prefix=api)
app.include_router(catalog.router, prefix=api)
app.include_router(inventory.router, prefix=api)
app.include_router(procurement.router, prefix=api)
app.include_router(customers.router, prefix=api)
app.include_router(sales.router, prefix=api)
app.include_router(transfers.router, prefix=api)
app.include_router(recovery.router, prefix=api)
app.include_router(pos.router, prefix=api)
app.include_router(mobile.router, prefix=api)
app.include_router(reports.router, prefix=api)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    try:
        async with asyncio.timeout(5):
            report = await check_database_readiness(engine)
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        log_failure(request, exc)
        raise ApiError(
            503, "database_unavailable", "The database is currently unavailable"
        ) from None
    if not report.ready:
        raise ApiError(
            503, "database_not_ready", "The database setup is not ready for application requests"
        )
    return {"status": "ready", "migration": report.migration}
