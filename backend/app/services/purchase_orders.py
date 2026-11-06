"""Purchase-order approval and physical-only, exactly-once receiving."""

import hashlib
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as inventory_db
from app.models import procurement as db
from app.services.catalog_rules import exact_stock_quantity
from app.services.opening_stock import InventoryPostingService
from app.services.procurement import ProcurementService

APPROVAL_LIMIT = Decimal("50000.0000")
ZERO = Decimal(0)


class PurchaseOrderService(ProcurementService):
    def __init__(self, session, context):
        super().__init__(session, context)
        self.posting = InventoryPostingService(session, context)

    async def statuses(self, kind):
        values = (
            [
                "DRAFT",
                "PENDING_APPROVAL",
                "APPROVED",
                "SENT",
                "PARTIALLY_RECEIVED",
                "RECEIVED",
                "CLOSED",
                "CANCELLED",
            ]
            if kind == "PURCHASE_ORDER"
            else ["DRAFT", "POSTED", "CANCELLED"]
        )
        for number, code in enumerate(values, 1):
            await self.session.execute(
                pg_insert(db.document_statuses)
                .values(
                    tenant_id=self.context.tenant_id,
                    document_kind=kind,
                    code=code,
                    name=code.replace("_", " ").title(),
                    sort_order=number,
                    is_initial=code == "DRAFT",
                    is_terminal=code in {"POSTED", "CLOSED", "CANCELLED"},
                    allows_edit=code not in {"POSTED", "CLOSED", "CANCELLED"},
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "document_kind", "code"])
            )

    async def number(self, kind, business_date: date):
        tenant = await self.catalog.row(catalog_db.tenants, "tenant_id", self.context.tenant_id)
        start = business_date.year - (business_date.month < tenant["fiscal_year_start_month"])
        year = f"{start}-{(start + 1) % 100:02d}"
        prefix = "PO" if kind == "PURCHASE_ORDER" else "GRN"
        row = (
            (
                await self.session.execute(
                    pg_insert(inventory_db.number_sequences)
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
                        set_={"next_value": inventory_db.number_sequences.c.next_value + 1},
                    )
                    .returning(inventory_db.number_sequences)
                )
            )
            .mappings()
            .one()
        )
        sequence = str(row["next_value"] - 1).zfill(row["padding"])
        return f"{row['prefix']}/{year}/{sequence}{row['suffix']}"

    async def location(self, location_id, kind):
        location = await self.catalog.reference(inventory_db.locations, "location_id", location_id)
        location_type = await self.catalog.row(
            inventory_db.location_types, "location_type_id", location["location_type_id"]
        )
        allowed = (
            {"WAREHOUSE", "GODOWN", "FACTORY"}
            if kind == "PURCHASE_ORDER"
            else {"WAREHOUSE", "GODOWN", "FACTORY", "DEPARTMENT_STORE"}
        )
        if (
            location_type["code"].upper() not in allowed
            or not location_type["is_storage"]
            or location_type["is_in_transit"]
        ):
            raise ApiError(
                403, "purchase_location_forbidden", "This location cannot receive purchases"
            )
        explicit = await self.session.scalar(
            select(db.location_document_rules.c.is_allowed).where(
                self.scope(db.location_document_rules),
                db.location_document_rules.c.location_type_id == location["location_type_id"],
                db.location_document_rules.c.document_kind == kind,
            )
        )
        if explicit is False:
            raise ApiError(
                403, "purchase_location_forbidden", "This location cannot receive purchases"
            )
        return location

    async def order(self, order_id, *, lock=False):
        order = await self.catalog.row(db.purchase_orders, "purchase_order_id", order_id, lock=lock)
        lines = await self.session.execute(
            select(db.purchase_order_lines)
            .where(
                self.scope(db.purchase_order_lines),
                db.purchase_order_lines.c.purchase_order_id == order_id,
            )
            .order_by(db.purchase_order_lines.c.line_no)
        )
        return {**order, "lines": [dict(row) for row in lines.mappings()]}

    async def orders(self, page, page_size):
        count = await self.session.scalar(
            select(func.count())
            .select_from(db.purchase_orders)
            .where(self.scope(db.purchase_orders))
        )
        ids = (
            await self.session.execute(
                select(db.purchase_orders.c.purchase_order_id)
                .where(self.scope(db.purchase_orders))
                .order_by(db.purchase_orders.c.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        return {
            "items": [await self.order(value) for value in ids],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def order_lines(self, payload):
        rows, subtotal, discounts, taxes = [], ZERO, ZERO, ZERO
        for number, source in enumerate(payload.lines, 1):
            item = await self.catalog.reference(catalog_db.items, "item_id", source.item_id)
            kind = await self.catalog.reference(
                catalog_db.item_types, "item_type_id", item["item_type_id"]
            )
            if (
                item["status"] != "ACTIVE"
                or item["item_kind"] == "COMPOSITE"
                or not kind["is_stockable"]
            ):
                raise ApiError(422, "purchase_item_invalid", "Choose an active stockable component")
            if any(
                item[key] for key in ("track_batch", "track_lot", "track_serial", "track_expiry")
            ):
                raise ApiError(
                    422, "purchase_tracking_required", "Tracked items need their receiving workflow"
                )
            unit = await self.catalog.reference(catalog_db.units, "uom_id", source.uom_id)
            stock_unit = await self.catalog.reference(
                catalog_db.units, "uom_id", item["stock_uom_id"]
            )
            factor = await self.catalog.factor_for(item, source.uom_id)
            exact_stock_quantity(source.quantity, Decimal(1), unit["decimal_places"])
            stock_qty = exact_stock_quantity(source.quantity, factor, stock_unit["decimal_places"])
            gross = (source.quantity * source.unit_price).quantize(Decimal("0.0001"))
            if source.discount_amount > gross:
                raise ApiError(422, "purchase_discount_invalid", "Discount exceeds the line value")
            taxable = gross - source.discount_amount
            tax = (taxable * source.tax_rate / 100).quantize(Decimal("0.0001"))
            rows.append(
                {
                    "purchase_order_line_id": uuid.uuid4(),
                    "tenant_id": self.context.tenant_id,
                    "line_no": number,
                    "item_id": source.item_id,
                    "uom_id": source.uom_id,
                    "quantity": source.quantity,
                    "quantity_in_stock_uom": stock_qty,
                    "unit_price": source.unit_price,
                    "discount_amount": source.discount_amount,
                    "tax_rate": source.tax_rate,
                    "tax_amount": tax,
                    "line_total": taxable + tax,
                    "expected_date": payload.expected_date,
                }
            )
            subtotal += gross
            discounts += source.discount_amount
            taxes += tax
        return rows, subtotal, discounts, taxes

    async def save_order(self, payload, order_id=None):
        self.authorize()
        await self.catalog.lock()
        vendor = await self.catalog.reference(db.vendors, "vendor_id", payload.vendor_id)
        if not vendor["is_active"]:
            raise ApiError(422, "purchase_vendor_inactive", "Choose an active supplier")
        await self.location(payload.location_id, "PURCHASE_ORDER")
        rows, subtotal, discounts, taxes = await self.order_lines(payload)
        if order_id:
            old = await self.order(order_id, lock=True)
            if old["status"] != "DRAFT" or old["updated_at"] != payload.expected_updated_at:
                raise ApiError(409, "purchase_order_stale", "Reload the current draft")
            await self.session.execute(
                delete(db.purchase_order_lines).where(
                    self.scope(db.purchase_order_lines),
                    db.purchase_order_lines.c.purchase_order_id == order_id,
                )
            )
            await self.session.execute(
                update(db.purchase_orders)
                .where(
                    self.scope(db.purchase_orders),
                    db.purchase_orders.c.purchase_order_id == order_id,
                )
                .values(
                    vendor_id=payload.vendor_id,
                    location_id=payload.location_id,
                    order_date=payload.order_date,
                    expected_date=payload.expected_date,
                    subtotal=subtotal,
                    discount_total=discounts,
                    tax_total=taxes,
                    grand_total=subtotal - discounts + taxes,
                    notes=payload.notes,
                )
            )
        else:
            await self.statuses("PURCHASE_ORDER")
            order_id = uuid.uuid4()
            await self.session.execute(
                insert(db.purchase_orders).values(
                    purchase_order_id=order_id,
                    tenant_id=self.context.tenant_id,
                    po_no=await self.number("PURCHASE_ORDER", payload.order_date),
                    status="DRAFT",
                    vendor_id=payload.vendor_id,
                    location_id=payload.location_id,
                    order_date=payload.order_date,
                    expected_date=payload.expected_date,
                    payment_terms_days=vendor["payment_terms_days"],
                    subtotal=subtotal,
                    discount_total=discounts,
                    tax_total=taxes,
                    grand_total=subtotal - discounts + taxes,
                    notes=payload.notes,
                    created_by=self.context.user_id,
                )
            )
        for row in rows:
            await self.session.execute(
                insert(db.purchase_order_lines).values(**row, purchase_order_id=order_id)
            )
        return await self.order(order_id)

    async def action(self, order_id, action, expected_updated_at):
        self.authorize()
        await self.catalog.lock()
        order = await self.order(order_id, lock=True)
        if order["updated_at"] != expected_updated_at:
            raise ApiError(409, "purchase_order_stale", "Reload the purchase order")
        values = {}
        if action == "submit" and order["status"] == "DRAFT":
            needs_approval = order["grand_total"] >= APPROVAL_LIMIT
            values = {
                "status": "PENDING_APPROVAL" if needs_approval else "APPROVED",
                "approval_state": "PENDING" if needs_approval else "NOT_REQUIRED",
            }
        elif action == "approve" and order["status"] == "PENDING_APPROVAL":
            values = {
                "status": "APPROVED",
                "approval_state": "APPROVED",
                "approved_by": self.context.user_id,
                "approved_at": func.clock_timestamp(),
            }
        elif action == "send" and order["status"] == "APPROVED":
            values = {"status": "SENT", "sent_at": func.clock_timestamp()}
        else:
            raise ApiError(
                409,
                "purchase_order_action_invalid",
                "This action is unavailable in the current status",
            )
        await self.session.execute(
            update(db.purchase_orders)
            .where(
                self.scope(db.purchase_orders),
                db.purchase_orders.c.purchase_order_id == order_id,
            )
            .values(**values)
        )
        return await self.order(order_id)

    async def amend(self, order_id, payload):
        self.authorize()
        await self.catalog.lock()
        order = await self.order(order_id, lock=True)
        if order["updated_at"] != payload.expected_updated_at or order["status"] not in {
            "SENT",
            "PARTIALLY_RECEIVED",
        }:
            raise ApiError(409, "purchase_amendment_stale", "Reload an active sent purchase order")
        line = next(
            (
                value
                for value in order["lines"]
                if value["purchase_order_line_id"] == payload.line_id
            ),
            None,
        )
        if not line or payload.new_quantity <= line["quantity"]:
            raise ApiError(
                422, "purchase_amendment_invalid", "An amendment must increase an existing line"
            )
        unit = await self.catalog.reference(catalog_db.units, "uom_id", line["uom_id"])
        exact_stock_quantity(payload.new_quantity, Decimal(1), unit["decimal_places"])
        item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"])
        stock_unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
        stock_qty = exact_stock_quantity(
            payload.new_quantity,
            await self.catalog.factor_for(item, line["uom_id"]),
            stock_unit["decimal_places"],
        )
        delta = payload.new_quantity - line["quantity"]
        gross = (delta * line["unit_price"]).quantize(Decimal("0.0001"))
        tax = (gross * line["tax_rate"] / 100).quantize(Decimal("0.0001"))
        await self.session.execute(
            update(db.purchase_order_lines)
            .where(
                self.scope(db.purchase_order_lines),
                db.purchase_order_lines.c.purchase_order_line_id == payload.line_id,
            )
            .values(
                quantity=payload.new_quantity,
                quantity_in_stock_uom=stock_qty,
                tax_amount=line["tax_amount"] + tax,
                line_total=line["line_total"] + gross + tax,
            )
        )
        await self.session.execute(
            update(db.purchase_orders)
            .where(
                self.scope(db.purchase_orders),
                db.purchase_orders.c.purchase_order_id == order_id,
            )
            .values(
                subtotal=order["subtotal"] + gross,
                tax_total=order["tax_total"] + tax,
                grand_total=order["grand_total"] + gross + tax,
                status="PENDING_APPROVAL",
                approval_state="PENDING",
                approved_at=None,
                approved_by=None,
                notes=(order["notes"] or "") + f"\nAmendment: {payload.reason}",
            )
        )
        return await self.order(order_id)

    async def receipt(self, receipt_id, *, lock=False):
        document = await self.catalog.row(
            db.purchase_receives, "purchase_receive_id", receipt_id, lock=lock
        )
        lines = await self.session.execute(
            select(db.purchase_receive_lines)
            .where(
                self.scope(db.purchase_receive_lines),
                db.purchase_receive_lines.c.purchase_receive_id == receipt_id,
            )
            .order_by(db.purchase_receive_lines.c.line_no)
        )
        return {**document, "lines": [dict(row) for row in lines.mappings()]}

    async def receipts(self, page, page_size):
        count = await self.session.scalar(
            select(func.count())
            .select_from(db.purchase_receives)
            .where(self.scope(db.purchase_receives))
        )
        ids = (
            await self.session.execute(
                select(db.purchase_receives.c.purchase_receive_id)
                .where(self.scope(db.purchase_receives))
                .order_by(db.purchase_receives.c.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        return {
            "items": [await self.receipt(value) for value in ids],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def save_receipt(self, payload):
        self.authorize()
        await self.catalog.lock()
        order = await self.order(payload.purchase_order_id, lock=True)
        if order["status"] not in {"SENT", "PARTIALLY_RECEIVED"}:
            raise ApiError(409, "purchase_not_sent", "Only a sent order can be received")
        await self.location(order["location_id"], "PURCHASE_RECEIVE")
        if len({line.purchase_order_line_id for line in payload.lines}) != len(payload.lines):
            raise ApiError(422, "receipt_duplicate_line", "Use one receipt line per order line")
        await self.statuses("PURCHASE_RECEIVE")
        receipt_id = uuid.uuid4()
        now = await self.session.scalar(select(func.clock_timestamp()))
        await self.session.execute(
            insert(db.purchase_receives).values(
                purchase_receive_id=receipt_id,
                tenant_id=self.context.tenant_id,
                receive_no=await self.number("PURCHASE_RECEIVE", now.date()),
                status="DRAFT",
                purchase_order_id=order["purchase_order_id"],
                vendor_id=order["vendor_id"],
                location_id=order["location_id"],
                notes=payload.notes,
                created_by=self.context.user_id,
                received_by=self.context.user_id,
                total_short_qty=sum((line.short_quantity for line in payload.lines), ZERO),
                total_damaged_qty=sum((line.damaged_quantity for line in payload.lines), ZERO),
                has_variance=any(
                    line.short_quantity or line.damaged_quantity for line in payload.lines
                ),
            )
        )
        for number, source in enumerate(payload.lines, 1):
            po_line = next(
                (
                    row
                    for row in order["lines"]
                    if row["purchase_order_line_id"] == source.purchase_order_line_id
                ),
                None,
            )
            if po_line is None:
                raise ApiError(
                    422, "receipt_line_invalid", "Receipt line must belong to its purchase order"
                )
            if (
                source.received_quantity + source.damaged_quantity + source.short_quantity
                > po_line["quantity"] - po_line["received_quantity"] - po_line["cancelled_quantity"]
            ):
                raise ApiError(
                    409,
                    "receipt_exceeds_order",
                    "Increase and approve the order before receiving excess goods",
                )
            item = await self.catalog.row(catalog_db.items, "item_id", po_line["item_id"])
            stock_unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            factor = await self.catalog.factor_for(item, po_line["uom_id"])
            stock_qty = (
                exact_stock_quantity(source.received_quantity, factor, stock_unit["decimal_places"])
                if source.received_quantity
                else ZERO
            )
            unit_cost = (po_line["unit_price"] / factor).quantize(Decimal("0.000001"))
            await self.session.execute(
                insert(db.purchase_receive_lines).values(
                    purchase_receive_line_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    purchase_receive_id=receipt_id,
                    line_no=number,
                    purchase_order_line_id=po_line["purchase_order_line_id"],
                    item_id=po_line["item_id"],
                    uom_id=po_line["uom_id"],
                    received_quantity=source.received_quantity,
                    damaged_quantity=source.damaged_quantity,
                    short_quantity=source.short_quantity,
                    quantity_in_stock_uom=stock_qty,
                    unit_cost=unit_cost,
                    landed_cost_total=(
                        (source.received_quantity + source.damaged_quantity) * po_line["unit_price"]
                    ).quantize(Decimal("0.0001")),
                )
            )
        return await self.receipt(receipt_id)

    async def quarantine(self, location):
        code = f"QUAR-{str(location['location_id'])[:8].upper()}"
        kind_id = await self.session.scalar(
            select(inventory_db.location_types.c.location_type_id).where(
                self.scope(inventory_db.location_types),
                inventory_db.location_types.c.code == "QUARANTINE",
            )
        )
        if kind_id is None:
            kind_id = uuid.uuid4()
            await self.session.execute(
                pg_insert(inventory_db.location_types)
                .values(
                    location_type_id=kind_id,
                    tenant_id=self.context.tenant_id,
                    code="QUARANTINE",
                    name="Quarantine",
                    is_storage=True,
                    is_in_transit=False,
                    is_system=True,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
            )
            kind_id = await self.session.scalar(
                select(inventory_db.location_types.c.location_type_id).where(
                    self.scope(inventory_db.location_types),
                    inventory_db.location_types.c.code == "QUARANTINE",
                )
            )
        await self.session.execute(
            pg_insert(inventory_db.locations)
            .values(
                location_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                location_type_id=kind_id,
                code=code,
                name=f"Quarantine · {location['code']}",
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        return await self.session.scalar(
            select(inventory_db.locations.c.location_id).where(
                self.scope(inventory_db.locations), inventory_db.locations.c.code == code
            )
        )

    async def assert_fifo_pair(self, item_id, location_id):
        layers = await self.session.scalar(
            select(
                func.coalesce(
                    func.sum(inventory_db.fifo_layers.c.remaining_qty),
                    0,
                )
            ).where(
                self.scope(inventory_db.fifo_layers),
                inventory_db.fifo_layers.c.item_id == item_id,
                inventory_db.fifo_layers.c.location_id == location_id,
            )
        )
        physical = await self.session.scalar(
            select(inventory_db.balances.c.physical_on_hand).where(
                self.scope(inventory_db.balances),
                inventory_db.balances.c.item_id == item_id,
                inventory_db.balances.c.location_id == location_id,
            )
        )
        if layers != (physical or ZERO):
            raise ApiError(409, "fifo_history_mismatch", "FIFO layers differ from physical stock")

    async def post_receipt(self, receipt_id, key):
        self.authorize()
        await self.catalog.lock()
        fingerprint = hashlib.sha256(f"purchase_receive:{receipt_id}".encode()).hexdigest()
        existing = (
            (
                await self.session.execute(
                    select(inventory_db.idempotency_keys)
                    .where(
                        self.scope(inventory_db.idempotency_keys),
                        inventory_db.idempotency_keys.c.key == key,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["request_fingerprint"] != fingerprint or existing["scope"] != "POSTING":
                raise ApiError(
                    409, "receipt_key_reused", "This receipt key belongs to another action"
                )
            if existing["response_body"]:
                return await self.receipt(receipt_id)
        else:
            await self.session.execute(
                insert(inventory_db.idempotency_keys).values(
                    tenant_id=self.context.tenant_id,
                    key=key,
                    scope="POSTING",
                    request_fingerprint=fingerprint,
                )
            )
        document = await self.receipt(receipt_id, lock=True)
        if document["status"] != "DRAFT":
            raise ApiError(409, "receipt_already_posted", "This receipt is no longer a draft")
        order = await self.order(document["purchase_order_id"], lock=True)
        if order["status"] not in {"SENT", "PARTIALLY_RECEIVED"}:
            raise ApiError(409, "purchase_not_sent", "The purchase order is not ready to receive")
        location = await self.location(document["location_id"], "PURCHASE_RECEIVE")
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        pairs = []
        for line in document["lines"]:
            po_line = next(
                row
                for row in order["lines"]
                if row["purchase_order_line_id"] == line["purchase_order_line_id"]
            )
            total = line["received_quantity"] + line["damaged_quantity"]
            outstanding = (
                po_line["quantity"] - po_line["received_quantity"] - po_line["cancelled_quantity"]
            )
            if total + line["short_quantity"] > outstanding:
                raise ApiError(
                    409,
                    "receipt_exceeds_order",
                    "Increase and approve the order before receiving excess goods",
                )
            item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"], lock=True)
            if item["status"] != "ACTIVE" or any(
                item[key] for key in ("track_batch", "track_lot", "track_serial", "track_expiry")
            ):
                raise ApiError(
                    409, "receipt_item_changed", "The item is not ready for this receiving workflow"
                )
            stock_unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            factor = await self.catalog.factor_for(item, line["uom_id"])
            good_qty = (
                exact_stock_quantity(
                    line["received_quantity"], factor, stock_unit["decimal_places"]
                )
                if line["received_quantity"]
                else ZERO
            )
            damaged_qty = (
                exact_stock_quantity(line["damaged_quantity"], factor, stock_unit["decimal_places"])
                if line["damaged_quantity"]
                else ZERO
            )
            if good_qty != line["quantity_in_stock_uom"]:
                raise ApiError(
                    409, "receipt_catalog_changed", "Item conversion changed; recreate the receipt"
                )
            group_method = (
                await self.session.scalar(
                    select(catalog_db.groups.c.valuation_method).where(
                        self.scope(catalog_db.groups),
                        catalog_db.groups.c.item_group_id == item["item_group_id"],
                    )
                )
                if item["item_group_id"]
                else None
            )
            method = (
                item["valuation_method"]
                or group_method
                or await self.session.scalar(
                    select(catalog_db.tenants.c.default_valuation_method).where(
                        self.scope(catalog_db.tenants)
                    )
                )
            )
            if item["valuation_locked_at"] is None and total:
                await self.session.execute(
                    update(catalog_db.items)
                    .where(
                        self.scope(catalog_db.items),
                        catalog_db.items.c.item_id == item["item_id"],
                    )
                    .values(
                        valuation_method=method,
                        valuation_locked_at=occurred_at,
                        catalog_version=item["catalog_version"] + 1,
                    )
                )
            if good_qty:
                if method == "FIFO":
                    await self.assert_fifo_pair(line["item_id"], document["location_id"])
                entry_id = await self.posting.append_purchase_receipt(
                    document,
                    line,
                    document["location_id"],
                    good_qty,
                    line["unit_cost"],
                    method,
                    occurred_at,
                    key,
                )
                await self.session.execute(
                    update(db.purchase_receive_lines)
                    .where(
                        self.scope(db.purchase_receive_lines),
                        db.purchase_receive_lines.c.purchase_receive_line_id
                        == line["purchase_receive_line_id"],
                    )
                    .values(ledger_entry_id=entry_id)
                )
                pairs.append((line["item_id"], document["location_id"]))
            if damaged_qty:
                quarantine_id = await self.quarantine(location)
                if method == "FIFO":
                    await self.assert_fifo_pair(line["item_id"], quarantine_id)
                await self.posting.append_purchase_receipt(
                    document,
                    line,
                    quarantine_id,
                    damaged_qty,
                    line["unit_cost"],
                    method,
                    occurred_at,
                    key,
                )
                pairs.append((line["item_id"], quarantine_id))
            if total:
                await self.session.execute(
                    update(db.purchase_order_lines)
                    .where(
                        self.scope(db.purchase_order_lines),
                        db.purchase_order_lines.c.purchase_order_line_id
                        == po_line["purchase_order_line_id"],
                    )
                    .values(received_quantity=po_line["received_quantity"] + total)
                )
        await self.session.execute(
            update(db.purchase_receives)
            .where(
                self.scope(db.purchase_receives),
                db.purchase_receives.c.purchase_receive_id == receipt_id,
            )
            .values(status="POSTED", received_at=occurred_at)
        )
        total_received = sum((row["received_quantity"] for row in order["lines"]), ZERO) + sum(
            (row["received_quantity"] + row["damaged_quantity"] for row in document["lines"]), ZERO
        )
        total_ordered = sum(
            (row["quantity"] - row["cancelled_quantity"] for row in order["lines"]), ZERO
        )
        status = (
            "RECEIVED"
            if total_received >= total_ordered
            else "PARTIALLY_RECEIVED"
            if total_received
            else "SENT"
        )
        await self.session.execute(
            update(db.purchase_orders)
            .where(
                self.scope(db.purchase_orders),
                db.purchase_orders.c.purchase_order_id == order["purchase_order_id"],
            )
            .values(status=status)
        )
        if pairs:
            await self.session.execute(
                insert(inventory_db.outbox_events).values(
                    outbox_event_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    event_type="inventory.purchase_received",
                    aggregate_id=receipt_id,
                    payload={
                        "purchase_receive_id": str(receipt_id),
                        "item_locations": [
                            {"item_id": str(item_id), "location_id": str(location_id)}
                            for item_id, location_id in sorted(set(pairs))
                        ],
                    },
                    status="PENDING",
                )
            )
        await self.session.execute(
            update(inventory_db.idempotency_keys)
            .where(
                self.scope(inventory_db.idempotency_keys),
                inventory_db.idempotency_keys.c.key == key,
            )
            .values(response_status=200, response_body={"purchase_receive_id": str(receipt_id)})
        )
        return await self.receipt(receipt_id)
