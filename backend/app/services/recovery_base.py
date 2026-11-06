"""Shared transaction helpers for physical returns and stock counts."""

import hashlib
import json
import uuid
from datetime import date

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as stock
from app.models import procurement as procurement_db
from app.models import recovery as db
from app.services.catalog import CatalogService
from app.services.opening_stock import InventoryPostingService


class RecoveryBase:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)
        self.posting = InventoryPostingService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    def authorize(self):
        if not self.context.local and self.context.role not in {"Owner", "Admin"}:
            raise ApiError(
                403, "stock_access_pending", "Stock access is not configured for this role"
            )
        if not self.context.can_write:
            raise ApiError(403, "stock_write_forbidden", "Only an Owner or Admin can post stock")

    async def statuses(self, kind, values):
        for position, code in enumerate(values, 1):
            await self.session.execute(
                pg_insert(procurement_db.document_statuses)
                .values(
                    tenant_id=self.context.tenant_id,
                    document_kind=kind,
                    code=code,
                    name=code.replace("_", " ").title(),
                    sort_order=position,
                    is_initial=position == 1,
                    is_terminal=code in {"POSTED", "CLOSED", "CANCELLED", "VOID"},
                    allows_edit=code not in {"POSTED", "CLOSED", "CANCELLED", "VOID"},
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "document_kind", "code"])
            )

    async def number(self, kind, prefix):
        tenant = await self.catalog.row(catalog_db.tenants, "tenant_id", self.context.tenant_id)
        today = date.today()
        start = today.year - (today.month < tenant["fiscal_year_start_month"])
        year = f"{start}-{(start + 1) % 100:02d}"
        row = (
            (
                await self.session.execute(
                    pg_insert(stock.number_sequences)
                    .values(
                        tenant_id=self.context.tenant_id,
                        document_kind=kind,
                        fiscal_year=year,
                        prefix=prefix,
                        next_value=2,
                        padding=5,
                        suffix="",
                    )
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "document_kind", "fiscal_year"],
                        set_={"next_value": stock.number_sequences.c.next_value + 1},
                    )
                    .returning(stock.number_sequences)
                )
            )
            .mappings()
            .one()
        )
        sequence = str(row["next_value"] - 1).zfill(row["padding"])
        return f"{row['prefix']}/{year}/{sequence}{row['suffix']}"

    async def actor(self):
        if self.context.user_id is not None:
            return self.context.user_id
        actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"cims.local.actor:{self.context.tenant_id}")
        await self.session.execute(
            pg_insert(db.actors)
            .values(
                user_id=actor_id,
                tenant_id=self.context.tenant_id,
                email="local-stock-approval@cims.invalid",
                full_name="Local stock workspace",
                is_tenant_owner=True,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        return actor_id

    async def key(self, key, action, details):
        fingerprint = hashlib.sha256(
            json.dumps({"action": action, **details}, sort_keys=True).encode()
        ).hexdigest()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cims.idempotency:{self.context.tenant_id}:{key}"},
        )
        prior = (
            (
                await self.session.execute(
                    select(stock.idempotency_keys)
                    .where(self.scope(stock.idempotency_keys), stock.idempotency_keys.c.key == key)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["scope"] != "POSTING" or prior["request_fingerprint"] != fingerprint:
                raise ApiError(
                    409, "idempotency_conflict", "This operation key was used with different input"
                )
            if prior["response_body"] is None:
                raise ApiError(409, "operation_in_progress", "This operation has not completed")
            return prior["response_body"]
        await self.session.execute(
            insert(stock.idempotency_keys).values(
                tenant_id=self.context.tenant_id,
                key=key,
                scope="POSTING",
                request_fingerprint=fingerprint,
            )
        )
        return None

    async def finish(self, key, document, schema):
        body = schema.model_validate(document).model_dump(mode="json")
        await self.session.execute(
            update(stock.idempotency_keys)
            .where(self.scope(stock.idempotency_keys), stock.idempotency_keys.c.key == key)
            .values(response_status=200, response_body=body)
        )
        return document

    async def event(self, event_type, document_id, item_locations):
        await self.session.execute(
            insert(stock.outbox_events).values(
                outbox_event_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                event_type=event_type,
                aggregate_id=document_id,
                status="PENDING",
                payload={
                    "item_locations": [
                        {"item_id": str(item_id), "location_id": str(location_id)}
                        for item_id, location_id in sorted(item_locations)
                    ]
                },
            )
        )
