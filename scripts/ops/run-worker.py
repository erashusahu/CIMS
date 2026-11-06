"""Process one tenant's committed outbox jobs using PostgreSQL row locks."""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.api.catalog_access import CatalogContext
from app.db.base import SessionLocal, engine
from app.services.catalog import CatalogService
from app.services.outbox import OutboxWorker


async def run(tenant_id, loop):
    try:
        while True:
            async with SessionLocal() as session:
                context = CatalogContext(
                    tenant_id, None, "Owner", f"worker-{uuid.uuid4()}"
                )
                await CatalogService(session, context).prepare()
                result = await OutboxWorker(session, context).run_once()
                await session.commit()
                if result:
                    print(f"{result['event_id']}: {result['status']}")
                elif not loop:
                    print("No pending jobs for this workspace.")
            if not loop:
                return
            if result is None:
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, type=uuid.UUID)
    parser.add_argument("--loop", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.tenant, arguments.loop))
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # noqa: BLE001 - report every worker failure safely
        print(
            f"Worker stopped ({type(exc).__name__}); uncommitted jobs remain retryable."
        )
        raise SystemExit(1) from None
