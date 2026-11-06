"""Shipment-linked physical returns. Financial credits belong to the billing system."""

import uuid
from decimal import Decimal

from sqlalchemy import func, insert, select, update

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as stock
from app.models import recovery as db
from app.models import sales as sales_db
from app.schemas.recovery import ReturnOut
from app.services.catalog_rules import exact_stock_quantity
from app.services.purchase_orders import PurchaseOrderService
from app.services.recovery_base import RecoveryBase

ZERO = Decimal(0)


class ReturnService(RecoveryBase):
    async def shipped_options(self):
        shipments = (
            (
                await self.session.execute(
                    select(sales_db.shipments)
                    .where(self.scope(sales_db.shipments), sales_db.shipments.c.status == "SHIPPED")
                    .order_by(sales_db.shipments.c.shipped_at.desc())
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        result = []
        for shipment in shipments:
            lines = (
                (
                    await self.session.execute(
                        select(sales_db.shipment_lines).where(
                            self.scope(sales_db.shipment_lines),
                            sales_db.shipment_lines.c.shipment_id == shipment["shipment_id"],
                        )
                    )
                )
                .mappings()
                .all()
            )
            physical = {}
            for line in lines:
                item_id = line["item_id"]
                if item_id not in physical:
                    quantity = await self.shipped(shipment["shipment_id"], item_id)
                    if quantity:
                        physical[item_id] = quantity
            if not physical:
                continue
            result.append(
                {
                    "shipment_id": shipment["shipment_id"],
                    "shipment_no": shipment["shipment_no"],
                    "from_location_id": shipment["from_location_id"],
                    "lines": [
                        {"item_id": item_id, "quantity_in_stock_uom": quantity}
                        for item_id, quantity in physical.items()
                    ],
                }
            )
        return result

    async def document(self, return_id, *, lock=False):
        row = await self.catalog.row(db.returns, "sales_return_id", return_id, lock=lock)
        lines = (
            (
                await self.session.execute(
                    select(db.return_lines)
                    .where(
                        self.scope(db.return_lines), db.return_lines.c.sales_return_id == return_id
                    )
                    .order_by(db.return_lines.c.line_no)
                )
            )
            .mappings()
            .all()
        )
        return {**row, "lines": [dict(line) for line in lines]}

    async def list(self):
        ids = (
            (
                await self.session.execute(
                    select(db.returns.c.sales_return_id)
                    .where(self.scope(db.returns))
                    .order_by(db.returns.c.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [await self.document(return_id) for return_id in ids]

    async def shipped(self, shipment_id, item_id):
        return await self.session.scalar(
            select(func.coalesce(func.sum(stock.ledger.c.quantity), 0)).where(
                self.scope(stock.ledger),
                stock.ledger.c.source_document_type == "SHIPMENT",
                stock.ledger.c.source_document_id == shipment_id,
                stock.ledger.c.item_id == item_id,
                stock.ledger.c.stock_state == "PHYSICAL",
                stock.ledger.c.direction == "OUT",
            )
        )

    async def other_returns(self, shipment_id, item_id, exclude_id):
        return await self.session.scalar(
            select(func.coalesce(func.sum(db.return_lines.c.quantity_in_stock_uom), 0))
            .select_from(
                db.return_lines.join(
                    db.returns,
                    (db.return_lines.c.tenant_id == db.returns.c.tenant_id)
                    & (db.return_lines.c.sales_return_id == db.returns.c.sales_return_id),
                )
            )
            .where(
                self.scope(db.returns),
                db.returns.c.shipment_id == shipment_id,
                db.return_lines.c.item_id == item_id,
                db.returns.c.sales_return_id != exclude_id,
                db.returns.c.status.in_(["APPROVED", "RECEIVED", "CLOSED"]),
            )
        )

    async def validate_quantity(self, doc):
        for line in doc["lines"]:
            shipped = await self.shipped(doc["shipment_id"], line["item_id"])
            if not shipped:
                raise ApiError(
                    422,
                    "return_item_not_shipped",
                    "Choose an item physically shipped in this shipment",
                )
            existing = await self.other_returns(
                doc["shipment_id"], line["item_id"], doc["sales_return_id"]
            )
            if existing + line["quantity_in_stock_uom"] > shipped:
                raise ApiError(
                    409, "return_exceeds_shipped", "Cumulative returns exceed shipped stock"
                )

    async def create(self, payload):
        self.authorize()
        await self.catalog.lock()
        shipment = await self.catalog.row(
            sales_db.shipments, "shipment_id", payload.shipment_id, lock=True
        )
        if shipment["status"] != "SHIPPED":
            raise ApiError(409, "shipment_not_shipped", "Only a shipped document can be returned")
        order = await self.catalog.row(
            sales_db.orders, "sales_order_id", shipment["sales_order_id"]
        )
        location = await self.catalog.reference(
            stock.locations, "location_id", shipment["from_location_id"]
        )
        shipped_lines = (
            (
                await self.session.execute(
                    select(sales_db.shipment_lines).where(
                        self.scope(sales_db.shipment_lines),
                        sales_db.shipment_lines.c.shipment_id == payload.shipment_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        item_ids = {line["item_id"] for line in shipped_lines}
        for line in payload.lines:
            if line.item_id not in item_ids:
                raise ApiError(422, "return_item_not_shipped", "Choose an item in this shipment")
            item = await self.catalog.row(catalog_db.items, "item_id", line.item_id)
            if any(
                item[field]
                for field in ("track_batch", "track_lot", "track_serial", "track_expiry")
            ):
                raise ApiError(
                    422, "tracked_return_pending", "Tracked returns need batch or serial details"
                )
            unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            exact_stock_quantity(line.quantity, Decimal(1), unit["decimal_places"])
        await self.statuses(
            "SALES_RETURN", ("DRAFT", "APPROVED", "RECEIVED", "CLOSED", "CANCELLED")
        )
        return_id = uuid.uuid4()
        await self.session.execute(
            insert(db.returns).values(
                sales_return_id=return_id,
                tenant_id=self.context.tenant_id,
                return_no=await self.number("SALES_RETURN", "RMA"),
                status="DRAFT",
                customer_id=order["customer_id"],
                shipment_id=payload.shipment_id,
                location_id=location["location_id"],
                restock_location_id=location["location_id"],
                return_date=func.current_date(),
                inspection_required=True,
                total_quantity=sum((line.quantity for line in payload.lines), ZERO),
                reason_note=payload.reason_note,
                created_by=self.context.user_id,
            )
        )
        for position, line in enumerate(payload.lines, 1):
            item = await self.catalog.row(catalog_db.items, "item_id", line.item_id)
            await self.session.execute(
                insert(db.return_lines).values(
                    sales_return_line_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    sales_return_id=return_id,
                    line_no=position,
                    item_id=line.item_id,
                    uom_id=item["stock_uom_id"],
                    quantity=line.quantity,
                    quantity_in_stock_uom=line.quantity,
                    condition=line.condition,
                    restock=line.condition == "GOOD",
                )
            )
        doc = await self.document(return_id)
        await self.validate_quantity(doc)
        return doc

    async def approve(self, return_id, key):
        self.authorize()
        await self.catalog.lock()
        previous = await self.key(key, "return.approve", {"return": str(return_id)})
        if previous:
            return previous
        doc = await self.document(return_id, lock=True)
        if doc["status"] != "DRAFT":
            raise ApiError(409, "return_not_draft", "Only a draft return can be approved")
        await self.catalog.row(sales_db.shipments, "shipment_id", doc["shipment_id"], lock=True)
        await self.validate_quantity(doc)
        now = await self.session.scalar(select(func.clock_timestamp()))
        await self.session.execute(
            update(db.returns)
            .where(self.scope(db.returns), db.returns.c.sales_return_id == return_id)
            .values(status="APPROVED", approved_by=await self.actor(), approved_at=now)
        )
        return await self.finish(key, await self.document(return_id), ReturnOut)

    async def receive(self, return_id, key):
        self.authorize()
        await self.catalog.lock()
        previous = await self.key(key, "return.receive", {"return": str(return_id)})
        if previous:
            return previous
        doc = await self.document(return_id, lock=True)
        if doc["status"] != "APPROVED":
            raise ApiError(
                409, "return_not_approved", "Approve the inspected return before receiving"
            )
        await self.catalog.row(sales_db.shipments, "shipment_id", doc["shipment_id"], lock=True)
        await self.validate_quantity(doc)
        now = await self.session.scalar(select(func.clock_timestamp()))
        restock = await self.catalog.reference(
            stock.locations, "location_id", doc["restock_location_id"]
        )
        quarantine_id = None
        if any(line["condition"] != "GOOD" for line in doc["lines"]):
            quarantine_id = await PurchaseOrderService(self.session, self.context).quarantine(
                restock
            )
        affected = set()
        for line in doc["lines"]:
            item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"])
            method = item["valuation_method"] or await self.session.scalar(
                select(catalog_db.tenants.c.default_valuation_method).where(
                    self.scope(catalog_db.tenants)
                )
            )
            destination = restock["location_id"] if line["condition"] == "GOOD" else quarantine_id
            issues = (
                (
                    await self.session.execute(
                        select(stock.ledger)
                        .where(
                            self.scope(stock.ledger),
                            stock.ledger.c.source_document_type == "SHIPMENT",
                            stock.ledger.c.source_document_id == doc["shipment_id"],
                            stock.ledger.c.item_id == line["item_id"],
                            stock.ledger.c.stock_state == "PHYSICAL",
                            stock.ledger.c.direction == "OUT",
                        )
                        .order_by(stock.ledger.c.occurred_at, stock.ledger.c.entry_id)
                    )
                )
                .mappings()
                .all()
            )
            remaining = line["quantity_in_stock_uom"]
            for issue in issues:
                already = await self.session.scalar(
                    select(func.coalesce(func.sum(stock.ledger.c.quantity), 0)).where(
                        self.scope(stock.ledger),
                        stock.ledger.c.reversal_of_entry_id == issue["entry_id"],
                        stock.ledger.c.source_document_type == "SALES_RETURN",
                    )
                )
                take = min(remaining, issue["quantity"] - already)
                if take <= 0:
                    continue
                entry = await self.posting.append_recovery_movement(
                    code="SALES_RETURN_IN",
                    name="Sales return",
                    direction="IN",
                    item_id=line["item_id"],
                    location_id=destination,
                    quantity=take,
                    unit_cost=issue["unit_cost"] or ZERO,
                    document_kind="SALES_RETURN",
                    document_id=return_id,
                    line_id=line["sales_return_line_id"],
                    document_no=doc["return_no"],
                    occurred_at=now,
                    key=key,
                    reversal_of_entry_id=issue["entry_id"],
                )
                if method == "FIFO":
                    await self.session.execute(
                        insert(stock.fifo_layers).values(
                            tenant_id=self.context.tenant_id,
                            item_id=line["item_id"],
                            location_id=destination,
                            received_at=now,
                            original_qty=take,
                            remaining_qty=take,
                            unit_cost=issue["unit_cost"] or ZERO,
                            source_ledger_entry_id=entry.entry_id,
                            source_ledger_entry_at=entry.occurred_at,
                        )
                    )
                remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise ApiError(
                    409, "return_issue_mismatch", "Shipment issue history cannot cover this return"
                )
            affected.add((line["item_id"], destination))
        await self.session.execute(
            update(db.returns)
            .where(self.scope(db.returns), db.returns.c.sales_return_id == return_id)
            .values(status="RECEIVED", received_at=now, quarantine_location_id=quarantine_id)
        )
        await self.event("inventory.return_received", return_id, affected)
        return await self.finish(key, await self.document(return_id), ReturnOut)

    async def cancel(self, return_id):
        self.authorize()
        await self.catalog.lock()
        doc = await self.document(return_id, lock=True)
        if doc["status"] not in {"DRAFT", "APPROVED"}:
            raise ApiError(409, "return_not_cancelable", "Received returns cannot be cancelled")
        await self.session.execute(
            update(db.returns)
            .where(self.scope(db.returns), db.returns.c.sales_return_id == return_id)
            .values(status="CANCELLED")
        )
        return await self.document(return_id)
