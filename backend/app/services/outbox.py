import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from app.models import inventory as db
from app.services.inventory import InventoryService


class OutboxWorker:
    """A tenant-scoped, transactionally claimed local reconciliation worker."""

    def __init__(self, session, context):
        self.session, self.context = session, context

    async def run_once(self):
        now = await self.session.scalar(select(func.clock_timestamp()))
        event = (
            (
                await self.session.execute(
                    select(db.outbox_events)
                    .where(
                        db.outbox_events.c.tenant_id == self.context.tenant_id,
                        db.outbox_events.c.status.in_(["PENDING", "FAILED"]),
                        db.outbox_events.c.available_at <= now,
                    )
                    .order_by(db.outbox_events.c.available_at, db.outbox_events.c.outbox_event_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if event is None:
            return None
        attempts = event["attempts"] + 1
        error = None
        if event["event_type"] not in {
            "inventory.opening_posted",
            "inventory.purchase_received",
            "inventory.sales_reserved",
            "inventory.sales_released",
            "inventory.sales_shipped",
        }:
            error = "unsupported_event_type"
        else:
            try:
                values = event["payload"]["item_locations"]
                if not isinstance(values, list) or not 1 <= len(values) <= 10_000:
                    raise ValueError("Invalid pair list")
                pairs = [
                    (uuid.UUID(value["item_id"]), uuid.UUID(value["location_id"]))
                    for value in values
                ]
            except (ValueError, KeyError, TypeError, AttributeError):
                error = "invalid_event_payload"
            if error is None:
                differences = await InventoryService(self.session, self.context).reconcile(
                    1, 100, item_locations=pairs
                )
                if differences["total"]:
                    error = "stock_reconciliation_failed"
        # The row lock remains held through this database-only handler and final commit.
        # A crash rolls back the claim; no external side effect is marked done early.
        await self.session.execute(
            update(db.outbox_events)
            .where(
                db.outbox_events.c.tenant_id == self.context.tenant_id,
                db.outbox_events.c.outbox_event_id == event["outbox_event_id"],
            )
            .values(
                status="FAILED" if error else "DONE",
                attempts=attempts,
                available_at=now + timedelta(seconds=min(300, 2 ** min(attempts, 8))),
                completed_at=None if error else now,
                last_error_code=error,
            )
        )
        return {
            "event_id": str(event["outbox_event_id"]),
            "status": "FAILED" if error else "DONE",
            "error_code": error,
        }
