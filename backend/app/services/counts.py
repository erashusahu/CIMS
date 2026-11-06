"""Stock counts with a database-enforced location/item freeze and approved postings."""

import uuid
from decimal import Decimal

from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as stock
from app.models import recovery as db
from app.schemas.recovery import CountOut
from app.services.catalog_rules import exact_stock_quantity
from app.services.recovery_base import RecoveryBase
from app.services.valuation import FifoLayer, plan_fifo_issue

ZERO = Decimal(0)
SIX = Decimal("0.000001")


class CountService(RecoveryBase):
    async def document(self, count_id, *, lock=False, hide_blind=False):
        row = await self.catalog.row(db.counts, "audit_count_id", count_id, lock=lock)
        lines = [
            dict(line)
            for line in (
                await self.session.execute(
                    select(db.count_lines)
                    .where(self.scope(db.count_lines), db.count_lines.c.audit_count_id == count_id)
                    .order_by(db.count_lines.c.item_id)
                )
            )
            .mappings()
            .all()
        ]
        if hide_blind and row["blind_count"] and row["status"] == "IN_PROGRESS":
            for line in lines:
                line["system_quantity"] = None
        return {**row, "lines": lines}

    async def list(self):
        ids = (
            (
                await self.session.execute(
                    select(db.counts.c.audit_count_id)
                    .where(self.scope(db.counts))
                    .order_by(db.counts.c.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [await self.document(count_id, hide_blind=True) for count_id in ids]

    async def stockable(self, item_id):
        item = await self.catalog.reference(catalog_db.items, "item_id", item_id)
        kind = await self.catalog.row(catalog_db.item_types, "item_type_id", item["item_type_id"])
        if not kind["is_stockable"] or item["item_kind"] == "COMPOSITE":
            raise ApiError(422, "count_item_invalid", "Choose a stockable item")
        if any(
            item[field] for field in ("track_batch", "track_lot", "track_serial", "track_expiry")
        ):
            raise ApiError(
                422, "tracked_count_pending", "Tracked counts need batch or serial details"
            )
        return item

    async def create(self, payload):
        self.authorize()
        await self.catalog.lock()
        location = await self.catalog.reference(stock.locations, "location_id", payload.location_id)
        kind = await self.catalog.row(
            stock.location_types, "location_type_id", location["location_type_id"]
        )
        if not kind["is_storage"] or kind["is_in_transit"]:
            raise ApiError(422, "count_location_invalid", "Choose a storage location")
        for item_id in payload.item_ids:
            await self.stockable(item_id)
        await self.statuses(
            "AUDIT_COUNT", ("DRAFT", "IN_PROGRESS", "SUBMITTED", "APPROVED", "POSTED", "CANCELLED")
        )
        count_id = uuid.uuid4()
        await self.session.execute(
            insert(db.counts).values(
                audit_count_id=count_id,
                tenant_id=self.context.tenant_id,
                count_no=await self.number("AUDIT_COUNT", "CNT"),
                status="DRAFT",
                location_id=payload.location_id,
                count_type=payload.count_type,
                blind_count=payload.blind_count,
                freeze_location=payload.count_type == "FULL",
                notes=payload.notes,
                created_by=self.context.user_id,
            )
        )
        if payload.count_type == "CYCLE":
            for item_id in payload.item_ids:
                await self.session.execute(
                    insert(db.count_lines).values(
                        audit_count_line_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        audit_count_id=count_id,
                        item_id=item_id,
                        system_quantity=0,
                    )
                )
        return await self.document(count_id)

    async def freeze_lock(self, location_id):
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cims.count.freeze:{self.context.tenant_id}:{location_id}"},
        )

    async def start(self, count_id):
        self.authorize()
        await self.catalog.lock()
        doc = await self.document(count_id, lock=True)
        if doc["status"] != "DRAFT":
            raise ApiError(409, "count_not_draft", "Only a draft count can start")
        await self.freeze_lock(doc["location_id"])
        other = await self.session.scalar(
            select(func.count())
            .select_from(db.counts)
            .where(
                self.scope(db.counts),
                db.counts.c.location_id == doc["location_id"],
                db.counts.c.audit_count_id != count_id,
                db.counts.c.status.in_(["IN_PROGRESS", "SUBMITTED", "APPROVED"]),
            )
        )
        if other:
            raise ApiError(409, "count_already_active", "Another count is active at this location")
        if doc["count_type"] == "FULL":
            items = (
                (
                    await self.session.execute(
                        select(catalog_db.items.c.item_id)
                        .join(
                            catalog_db.item_types,
                            and_(
                                catalog_db.item_types.c.tenant_id == catalog_db.items.c.tenant_id,
                                catalog_db.item_types.c.item_type_id
                                == catalog_db.items.c.item_type_id,
                            ),
                        )
                        .where(
                            self.scope(catalog_db.items),
                            catalog_db.items.c.status == "ACTIVE",
                            catalog_db.item_types.c.is_stockable.is_(True),
                            catalog_db.items.c.item_kind != "COMPOSITE",
                        )
                        .order_by(catalog_db.items.c.item_id)
                    )
                )
                .scalars()
                .all()
            )
            if len(items) > 5000:
                raise ApiError(
                    422, "count_scope_too_large", "Split this location into smaller counts"
                )
            for item_id in items:
                await self.stockable(item_id)
                await self.session.execute(
                    insert(db.count_lines).values(
                        audit_count_line_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        audit_count_id=count_id,
                        item_id=item_id,
                        system_quantity=0,
                    )
                )
            doc = await self.document(count_id)
        if not doc["lines"]:
            raise ApiError(422, "count_scope_empty", "Add stockable items before starting")
        now = await self.session.scalar(select(func.clock_timestamp()))
        for line in doc["lines"]:
            await self.stockable(line["item_id"])
            quantity = (
                await self.session.scalar(
                    select(stock.balances.c.physical_on_hand).where(
                        self.scope(stock.balances),
                        stock.balances.c.item_id == line["item_id"],
                        stock.balances.c.location_id == doc["location_id"],
                    )
                )
                or ZERO
            )
            await self.session.execute(
                update(db.count_lines)
                .where(
                    self.scope(db.count_lines),
                    db.count_lines.c.audit_count_line_id == line["audit_count_line_id"],
                )
                .values(system_quantity=quantity)
            )
        await self.session.execute(
            update(db.counts)
            .where(self.scope(db.counts), db.counts.c.audit_count_id == count_id)
            .values(status="IN_PROGRESS", snapshot_at=now, started_at=now)
        )
        return await self.document(count_id, hide_blind=True)

    async def submit(self, count_id, payload):
        self.authorize()
        await self.catalog.lock()
        doc = await self.document(count_id, lock=True)
        if doc["status"] != "IN_PROGRESS":
            raise ApiError(409, "count_not_in_progress", "Only an active count can be submitted")
        observed = {line.item_id: line.counted_quantity for line in payload.lines}
        expected = {line["item_id"] for line in doc["lines"]}
        if set(observed) != expected or len(payload.lines) != len(expected):
            raise ApiError(
                422, "count_lines_incomplete", "Count every item in the frozen scope once"
            )
        now = await self.session.scalar(select(func.clock_timestamp()))
        for line in doc["lines"]:
            item = await self.stockable(line["item_id"])
            unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            amount = observed[line["item_id"]]
            if amount:
                exact_stock_quantity(amount, Decimal(1), unit["decimal_places"])
            await self.session.execute(
                update(db.count_lines)
                .where(
                    self.scope(db.count_lines),
                    db.count_lines.c.audit_count_line_id == line["audit_count_line_id"],
                )
                .values(counted_quantity=amount, counted_by=self.context.user_id, counted_at=now)
            )
        await self.session.execute(
            update(db.counts)
            .where(self.scope(db.counts), db.counts.c.audit_count_id == count_id)
            .values(status="SUBMITTED", submitted_at=now)
        )
        return await self.document(count_id)

    async def reason(self):
        await self.session.execute(
            pg_insert(db.reasons)
            .values(
                reason_code_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code="COUNT_VARIANCE",
                name="Stock count variance",
                applies_to="AUDIT_VARIANCE",
                requires_approval=True,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code", "applies_to"])
        )
        row = (
            (
                await self.session.execute(
                    select(db.reasons).where(
                        self.scope(db.reasons),
                        db.reasons.c.code == "COUNT_VARIANCE",
                        db.reasons.c.applies_to == "AUDIT_VARIANCE",
                    )
                )
            )
            .mappings()
            .one()
        )
        if not row["requires_approval"] or not row["is_active"]:
            raise ApiError(
                409, "count_reason_configuration", "Count variance reason requires review"
            )
        return row["reason_code_id"]

    async def approve(self, count_id):
        self.authorize()
        await self.catalog.lock()
        doc = await self.document(count_id, lock=True)
        if doc["status"] != "SUBMITTED":
            raise ApiError(409, "count_not_submitted", "Submit the count before approval")
        actor = await self.actor()
        now = await self.session.scalar(select(func.clock_timestamp()))
        await self.statuses(
            "ADJUSTMENT", ("DRAFT", "PENDING_APPROVAL", "APPROVED", "POSTED", "VOID")
        )
        reason_id = await self.reason()
        for line in doc["lines"]:
            delta = line["counted_quantity"] - line["system_quantity"]
            values = {"approved_by": actor, "approved_at": now}
            if delta:
                adjustment_id = uuid.uuid4()
                await self.session.execute(
                    insert(db.adjustments).values(
                        adjustment_id=adjustment_id,
                        tenant_id=self.context.tenant_id,
                        adjustment_no=await self.number("ADJUSTMENT", "ADJ"),
                        status="APPROVED",
                        location_id=doc["location_id"],
                        item_id=line["item_id"],
                        quantity_delta=delta,
                        reason_code_id=reason_id,
                        reason_note=f"Count {doc['count_no']} variance",
                        requested_by=self.context.user_id,
                        approved_by=actor,
                        approved_at=now,
                    )
                )
                values["adjustment_id"] = adjustment_id
                values["reason_code_id"] = reason_id
            await self.session.execute(
                update(db.count_lines)
                .where(
                    self.scope(db.count_lines),
                    db.count_lines.c.audit_count_line_id == line["audit_count_line_id"],
                )
                .values(**values)
            )
        await self.session.execute(
            update(db.counts)
            .where(self.scope(db.counts), db.counts.c.audit_count_id == count_id)
            .values(status="APPROVED", approved_by=actor, approved_at=now)
        )
        return await self.document(count_id)

    async def post(self, count_id, key):
        self.authorize()
        await self.catalog.lock()
        prior = await self.key(key, "count.post", {"count": str(count_id)})
        if prior:
            return prior
        doc = await self.document(count_id, lock=True)
        if doc["status"] != "APPROVED":
            raise ApiError(
                409, "count_not_approved", "Approve the count before posting adjustments"
            )
        await self.freeze_lock(doc["location_id"])
        now = await self.session.scalar(select(func.clock_timestamp()))
        affected = set()
        for line in doc["lines"]:
            delta = line["counted_quantity"] - line["system_quantity"]
            if delta == 0:
                continue
            item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"], lock=True)
            balance = (
                (
                    await self.session.execute(
                        select(stock.balances)
                        .where(
                            self.scope(stock.balances),
                            stock.balances.c.item_id == line["item_id"],
                            stock.balances.c.location_id == doc["location_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            physical = balance["physical_on_hand"] if balance else ZERO
            committed = balance["committed"] if balance else ZERO
            if physical != line["system_quantity"]:
                raise ApiError(409, "count_stock_changed", "Stock changed after the count snapshot")
            if line["counted_quantity"] < committed:
                raise ApiError(
                    409, "count_below_commitments", "Counted stock is below active reservations"
                )
            adjustment = await self.catalog.row(
                db.adjustments, "adjustment_id", line["adjustment_id"], lock=True
            )
            if adjustment["status"] != "APPROVED" or adjustment["quantity_delta"] != delta:
                raise ApiError(409, "count_adjustment_changed", "Approved adjustment needs review")
            method = item["valuation_method"] or await self.session.scalar(
                select(catalog_db.tenants.c.default_valuation_method).where(
                    self.scope(catalog_db.tenants)
                )
            )
            amount = abs(delta)
            allocations = []
            if method == "FIFO" and delta < 0:
                rows = (
                    (
                        await self.session.execute(
                            select(stock.fifo_layers)
                            .where(
                                self.scope(stock.fifo_layers),
                                stock.fifo_layers.c.item_id == line["item_id"],
                                stock.fifo_layers.c.location_id == doc["location_id"],
                                stock.fifo_layers.c.remaining_qty > 0,
                            )
                            .order_by(
                                stock.fifo_layers.c.received_at, stock.fifo_layers.c.fifo_layer_id
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                if sum((row["remaining_qty"] for row in rows), ZERO) != physical:
                    raise ApiError(
                        409, "fifo_history_mismatch", "FIFO layers differ from counted stock"
                    )
                allocations = plan_fifo_issue(
                    [
                        FifoLayer(
                            row["fifo_layer_id"],
                            row["received_at"],
                            row["remaining_qty"],
                            row["unit_cost"],
                        )
                        for row in rows
                    ],
                    amount,
                )
                cost = (
                    sum((part.quantity * part.unit_cost for part in allocations), ZERO) / amount
                ).quantize(SIX)
            else:
                cost = balance["avg_unit_cost"] if balance else item["cost_price"]
            entry = await self.posting.append_recovery_movement(
                code="ADJUSTMENT_UP" if delta > 0 else "ADJUSTMENT_DOWN",
                name="Count adjustment increase" if delta > 0 else "Count adjustment decrease",
                direction="IN" if delta > 0 else "OUT",
                item_id=line["item_id"],
                location_id=doc["location_id"],
                quantity=amount,
                unit_cost=cost,
                document_kind="ADJUSTMENT",
                document_id=adjustment["adjustment_id"],
                line_id=line["audit_count_line_id"],
                document_no=adjustment["adjustment_no"],
                occurred_at=now,
                key=key,
                reason_code_id=adjustment["reason_code_id"],
                reason_note=adjustment["reason_note"],
            )
            if method == "FIFO" and delta > 0:
                await self.session.execute(
                    insert(stock.fifo_layers).values(
                        tenant_id=self.context.tenant_id,
                        item_id=line["item_id"],
                        location_id=doc["location_id"],
                        received_at=now,
                        original_qty=amount,
                        remaining_qty=amount,
                        unit_cost=cost,
                        source_ledger_entry_id=entry.entry_id,
                        source_ledger_entry_at=entry.occurred_at,
                    )
                )
            for allocation in allocations:
                await self.session.execute(
                    update(stock.fifo_layers)
                    .where(
                        self.scope(stock.fifo_layers),
                        stock.fifo_layers.c.fifo_layer_id == allocation.layer_id,
                    )
                    .values(remaining_qty=stock.fifo_layers.c.remaining_qty - allocation.quantity)
                )
                await self.session.execute(
                    insert(stock.fifo_consumptions).values(
                        tenant_id=self.context.tenant_id,
                        fifo_layer_id=allocation.layer_id,
                        ledger_entry_id=entry.entry_id,
                        ledger_entry_at=entry.occurred_at,
                        quantity=allocation.quantity,
                        unit_cost=allocation.unit_cost,
                    )
                )
            await self.session.execute(
                update(db.adjustments)
                .where(
                    self.scope(db.adjustments),
                    db.adjustments.c.adjustment_id == adjustment["adjustment_id"],
                )
                .values(status="POSTED", posted_at=now, ledger_entry_id=entry.entry_id)
            )
            affected.add((line["item_id"], doc["location_id"]))
        await self.session.execute(
            update(db.counts)
            .where(self.scope(db.counts), db.counts.c.audit_count_id == count_id)
            .values(status="POSTED")
        )
        if affected:
            await self.event("inventory.count_posted", count_id, affected)
        return await self.finish(key, await self.document(count_id), CountOut)

    async def cancel(self, count_id):
        self.authorize()
        await self.catalog.lock()
        doc = await self.document(count_id, lock=True)
        if doc["status"] not in {"DRAFT", "IN_PROGRESS", "SUBMITTED", "APPROVED"}:
            raise ApiError(409, "count_not_cancelable", "Posted counts cannot be cancelled")
        if doc["status"] == "APPROVED":
            for line in doc["lines"]:
                if line["adjustment_id"]:
                    await self.session.execute(
                        update(db.adjustments)
                        .where(
                            self.scope(db.adjustments),
                            db.adjustments.c.adjustment_id == line["adjustment_id"],
                        )
                        .values(status="VOID")
                    )
        await self.session.execute(
            update(db.counts)
            .where(self.scope(db.counts), db.counts.c.audit_count_id == count_id)
            .values(status="CANCELLED")
        )
        return await self.document(count_id)
