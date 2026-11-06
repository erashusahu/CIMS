"""Measure the stock read route against the guarded disposable PostgreSQL cluster."""

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.catalog_access import CatalogContext, catalog_context
from app.db.base import get_session
from app.main import app
from app.models import inventory
from httpx import ASGITransport, AsyncClient


async def measure(requests: int, concurrency: int):
    url = make_url(os.environ.get("CIMS_CATALOG_TEST_URL", ""))
    if (
        url.host != "127.0.0.1"
        or url.port != 55439
        or url.database != "Inventroy"
        or url.username != "cims_catalog_test_app"
    ):
        raise RuntimeError("The workload may use only the isolated PostgreSQL test cluster")
    admin = create_async_engine(url.set(username="cims_test_admin"))
    engine = create_async_engine(url, pool_size=concurrency, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with admin.connect() as connection:
            tenant = await connection.scalar(
                select(inventory.balances.c.tenant_id)
                .group_by(inventory.balances.c.tenant_id)
                .order_by(text("count(*) DESC"))
                .limit(1)
            )
            if tenant is None:
                raise RuntimeError("The isolated cluster has no stock positions to measure")
            positions = await connection.scalar(
                select(text("count(*)"))
                .select_from(inventory.balances)
                .where(inventory.balances.c.tenant_id == tenant)
            )

        async def context_override():
            return CatalogContext(tenant, None, "Owner", "stock-load", local=True)

        async def session_override():
            async with sessions() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant, true)"),
                    {"tenant": str(tenant)},
                )
                yield session

        app.dependency_overrides[catalog_context] = context_override
        app.dependency_overrides[get_session] = session_override
        semaphore = asyncio.Semaphore(concurrency)
        durations = []
        errors = []
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:

            async def one():
                async with semaphore:
                    started = time.perf_counter()
                    try:
                        result = await client.get("/api/v1/inventory/balances?page_size=25")
                        if result.status_code != 200:
                            errors.append(result.status_code)
                    except Exception as exc:  # noqa: BLE001 - record every workload failure
                        errors.append(type(exc).__name__)
                    finally:
                        durations.append((time.perf_counter() - started) * 1000)

            await asyncio.gather(*(one() for _ in range(concurrency)))
            durations.clear()
            errors.clear()
            started = time.perf_counter()
            await asyncio.gather(*(one() for _ in range(requests)))
            elapsed = time.perf_counter() - started
        ordered = sorted(durations)
        return {
            "measured_at_utc": datetime.now(UTC).isoformat(),
            "route": "/api/v1/inventory/balances?page_size=25",
            "database": "isolated 127.0.0.1:55439/Inventroy",
            "stock_positions_for_tenant": positions,
            "requests": requests,
            "concurrency": concurrency,
            "errors": len(errors),
            "error_types": sorted({str(error) for error in errors}),
            "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
            "elapsed_seconds": round(elapsed, 2),
            "cpu_count": os.cpu_count(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "note": "Synthetic test data; not a production-scale acceptance measurement",
        }
    finally:
        app.dependency_overrides.pop(catalog_context, None)
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()
        await admin.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--output", type=Path, default=ROOT / ".tmp" / "stock-load-result.json")
    args = parser.parse_args()
    if args.requests < 1 or not 1 <= args.concurrency <= 50:
        parser.error("Requests must be positive and concurrency must be between 1 and 50")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("cims.request").setLevel(logging.WARNING)
    report = asyncio.run(measure(args.requests, args.concurrency))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
