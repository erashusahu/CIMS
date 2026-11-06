"""Phase 3C sales orders, ATP, credit exposure and atomic reservations."""

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import customer as customer_db
from app.models import inventory as inventory_db
from app.models import procurement as procurement_db
from app.models import sales as db
from app.services.catalog import CatalogService
from app.services.catalog_rules import exact_stock_quantity
from app.services.opening_stock import InventoryPostingService

ZERO = Decimal(0)
FOUR = Decimal("0.0001")
SIX = Decimal("0.000001")


class SalesOrderService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)
        self.posting = InventoryPostingService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    def authorize_read(self):
        if not self.context.local and self.context.role not in {"Owner", "Admin"}:
            raise ApiError(
                403, "sales_access_pending", "Sales access is not configured for this role"
            )

    def authorize_write(self):
        self.authorize_read()
        if not self.context.can_write:
            raise ApiError(
                403, "sales_write_forbidden", "Only an Owner or Admin can edit sales orders"
            )

    async def statuses(self):
        values = ("DRAFT", "CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED", "CANCELLED")
        for number, code in enumerate(values, 1):
            await self.session.execute(
                pg_insert(procurement_db.document_statuses)
                .values(
                    tenant_id=self.context.tenant_id,
                    document_kind="SALES_ORDER",
                    code=code,
                    name=code.replace("_", " ").title(),
                    sort_order=number,
                    is_initial=code == "DRAFT",
                    is_terminal=code in {"FULFILLED", "CANCELLED"},
                    allows_edit=code not in {"FULFILLED", "CANCELLED"},
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "document_kind", "code"])
            )

    async def number(self, business_date: date):
        tenant = await self.catalog.row(catalog_db.tenants, "tenant_id", self.context.tenant_id)
        start = business_date.year - (business_date.month < tenant["fiscal_year_start_month"])
        year = f"{start}-{(start + 1) % 100:02d}"
        row = (
            (
                await self.session.execute(
                    pg_insert(inventory_db.number_sequences)
                    .values(
                        tenant_id=self.context.tenant_id,
                        document_kind="SALES_ORDER",
                        fiscal_year=year,
                        prefix="SO",
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

    async def location(self, location_id):
        location = await self.catalog.reference(inventory_db.locations, "location_id", location_id)
        kind = await self.catalog.row(
            inventory_db.location_types, "location_type_id", location["location_type_id"]
        )
        if (
            kind["code"].upper() not in {"WAREHOUSE", "GODOWN", "STORE"}
            or not kind["is_storage"]
            or kind["is_in_transit"]
        ):
            raise ApiError(
                403, "sales_location_forbidden", "Choose a sales-enabled storage location"
            )
        explicit = await self.session.scalar(
            select(procurement_db.location_document_rules.c.is_allowed).where(
                self.scope(procurement_db.location_document_rules),
                procurement_db.location_document_rules.c.location_type_id
                == kind["location_type_id"],
                procurement_db.location_document_rules.c.document_kind == "SALES_ORDER",
            )
        )
        if explicit is False:
            raise ApiError(
                403, "sales_location_forbidden", "Sales are not allowed at this location"
            )
        return location

    async def order(self, order_id, *, lock=False):
        order = await self.catalog.row(db.orders, "sales_order_id", order_id, lock=lock)
        lines = await self.session.execute(
            select(db.lines)
            .where(
                self.scope(db.lines),
                db.lines.c.sales_order_id == order_id,
            )
            .order_by(db.lines.c.line_no)
        )
        reservations = await self.session.execute(
            select(db.reservations)
            .where(
                self.scope(db.reservations),
                db.reservations.c.sales_order_id == order_id,
            )
            .order_by(db.reservations.c.sales_order_line_id, db.reservations.c.item_id)
        )
        return {
            **order,
            "lines": [dict(row) for row in lines.mappings()],
            "reservations": [dict(row) for row in reservations.mappings()],
        }

    async def orders(self, page, page_size, source=None):
        conditions = [self.scope(db.orders)]
        if source:
            conditions.append(db.orders.c.sales_source == source)
        total = await self.session.scalar(
            select(func.count()).select_from(db.orders).where(*conditions)
        )
        ids = (
            await self.session.execute(
                select(db.orders.c.sales_order_id)
                .where(*conditions)
                .order_by(db.orders.c.created_at.desc(), db.orders.c.sales_order_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        return {
            "items": [await self.order(value) for value in ids],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def price_lines(self, payload):
        rows, subtotal, discounts, taxes = [], ZERO, ZERO, ZERO
        for number, source in enumerate(payload.lines, 1):
            item = await self.catalog.reference(catalog_db.items, "item_id", source.item_id)
            if item["status"] != "ACTIVE":
                raise ApiError(422, "sales_item_inactive", "Choose an active catalog item")
            location_id = source.location_id or payload.location_id
            await self.location(location_id)
            source_unit = await self.catalog.reference(catalog_db.units, "uom_id", source.uom_id)
            stock_unit = await self.catalog.reference(
                catalog_db.units, "uom_id", item["stock_uom_id"]
            )
            factor = await self.catalog.factor_for(item, source.uom_id)
            exact_stock_quantity(source.quantity, Decimal(1), source_unit["decimal_places"])
            stock_qty = exact_stock_quantity(source.quantity, factor, stock_unit["decimal_places"])
            unit_price = (item["selling_price"] * factor).quantize(SIX)
            gross = (source.quantity * unit_price).quantize(FOUR)
            discount = (gross * source.discount_pct / 100).quantize(FOUR)
            taxable = gross - discount
            rate = ZERO if item["is_tax_exempt"] else item["gst_rate"]
            tax = (taxable * rate / 100).quantize(FOUR)
            rows.append(
                {
                    "sales_order_line_id": uuid.uuid4(),
                    "tenant_id": self.context.tenant_id,
                    "line_no": number,
                    "item_id": source.item_id,
                    "uom_id": source.uom_id,
                    "location_id": location_id,
                    "quantity": source.quantity,
                    "quantity_in_stock_uom": stock_qty,
                    "unit_price": unit_price,
                    "discount_pct": source.discount_pct,
                    "discount_amount": discount,
                    "tax_rate": rate,
                    "taxable_value": taxable,
                    "tax_amount": tax,
                    "line_total": taxable + tax,
                    "cost_center_id": item["cost_center_id"],
                }
            )
            subtotal += gross
            discounts += discount
            taxes += tax
        return rows, subtotal, discounts, taxes

    async def save(self, payload, order_id=None):
        self.authorize_write()
        await self.catalog.lock()
        customer = await self.catalog.reference(
            customer_db.customers, "customer_id", payload.customer_id
        )
        await self.location(payload.location_id)
        rows, subtotal, discounts, taxes = await self.price_lines(payload)
        if order_id:
            old = await self.order(order_id, lock=True)
            if old["status"] != "DRAFT" or old["revision"] != payload.expected_revision:
                raise ApiError(409, "sales_order_stale", "Reload the current sales draft")
            if old["sales_source"] != payload.sales_source:
                raise ApiError(409, "sales_source_immutable", "Sales source cannot change")
            await self.session.execute(
                delete(db.lines).where(self.scope(db.lines), db.lines.c.sales_order_id == order_id)
            )
            await self.session.execute(
                update(db.orders)
                .where(
                    self.scope(db.orders),
                    db.orders.c.sales_order_id == order_id,
                )
                .values(
                    customer_id=payload.customer_id,
                    location_id=payload.location_id,
                    order_date=payload.order_date,
                    expected_ship_date=payload.expected_ship_date,
                    payment_terms_days=customer["payment_terms_days"],
                    place_of_supply_state=customer["place_of_supply_state"],
                    is_b2c=customer["party_type"] == "B2C",
                    subtotal=subtotal,
                    discount_total=discounts,
                    tax_total=taxes,
                    grand_total=subtotal - discounts + taxes,
                    notes=payload.notes,
                    revision=old["revision"] + 1,
                )
            )
        else:
            await self.statuses()
            order_id = uuid.uuid4()
            await self.session.execute(
                insert(db.orders).values(
                    sales_order_id=order_id,
                    tenant_id=self.context.tenant_id,
                    order_no=await self.number(payload.order_date),
                    status="DRAFT",
                    sales_source=payload.sales_source,
                    revision=1,
                    customer_id=payload.customer_id,
                    location_id=payload.location_id,
                    order_date=payload.order_date,
                    expected_ship_date=payload.expected_ship_date,
                    payment_terms_days=customer["payment_terms_days"],
                    place_of_supply_state=customer["place_of_supply_state"],
                    is_b2c=customer["party_type"] == "B2C",
                    subtotal=subtotal,
                    discount_total=discounts,
                    tax_total=taxes,
                    grand_total=subtotal - discounts + taxes,
                    notes=payload.notes,
                    created_by=self.context.user_id,
                )
            )
        for row in rows:
            await self.session.execute(insert(db.lines).values(**row, sales_order_id=order_id))
        return await self.order(order_id)

    async def requirements(self, item_id, stock_quantity, ancestors=()):
        if item_id in ancestors:
            raise ApiError(409, "kit_cycle", "Kit components contain a cycle")
        item = await self.catalog.reference(catalog_db.items, "item_id", item_id)
        if item["status"] != "ACTIVE":
            raise ApiError(409, "sales_item_changed", "An item is no longer active")
        if item["item_kind"] != "COMPOSITE":
            kind = await self.catalog.reference(
                catalog_db.item_types, "item_type_id", item["item_type_id"]
            )
            if not kind["is_stockable"]:
                raise ApiError(
                    422, "sales_item_not_stockable", "Only stockable items can be reserved"
                )
            if any(
                item[key] for key in ("track_batch", "track_lot", "track_serial", "track_expiry")
            ):
                raise ApiError(
                    422, "sales_tracking_required", "Tracked items need their allocation workflow"
                )
            return {item_id: stock_quantity}
        components = (
            (
                await self.session.execute(
                    select(catalog_db.components)
                    .where(
                        self.scope(catalog_db.components),
                        catalog_db.components.c.kit_item_id == item_id,
                    )
                    .order_by(catalog_db.components.c.sort_order)
                )
            )
            .mappings()
            .all()
        )
        if not components:
            raise ApiError(422, "kit_empty", "A virtual kit needs components before sale")
        expanded = defaultdict(lambda: ZERO)
        for component in components:
            child = await self.catalog.reference(
                catalog_db.items, "item_id", component["component_item_id"]
            )
            unit = await self.catalog.reference(catalog_db.units, "uom_id", child["stock_uom_id"])
            factor = await self.catalog.factor_for(child, component["uom_id"])
            entered = stock_quantity * component["quantity"] * (1 + component["scrap_pct"] / 100)
            child_qty = exact_stock_quantity(entered, factor, unit["decimal_places"])
            nested = await self.requirements(child["item_id"], child_qty, (*ancestors, item_id))
            for leaf_id, quantity in nested.items():
                expanded[leaf_id] += quantity
        return dict(expanded)

    async def availability(self, item_id, location_id):
        self.authorize_read()
        await self.location(location_id)
        item = await self.catalog.reference(catalog_db.items, "item_id", item_id)
        requirements = await self.requirements(item_id, Decimal(1))
        components, limits = [], []
        for leaf_id, required in sorted(requirements.items()):
            leaf = await self.catalog.row(catalog_db.items, "item_id", leaf_id)
            balance = (
                (
                    await self.session.execute(
                        select(inventory_db.balances).where(
                            self.scope(inventory_db.balances),
                            inventory_db.balances.c.item_id == leaf_id,
                            inventory_db.balances.c.location_id == location_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            available = max(
                ZERO,
                (balance["physical_on_hand"] - balance["committed"] if balance else ZERO)
                - leaf["safety_stock"],
            )
            components.append(
                {
                    "item_id": leaf_id,
                    "sku": leaf["sku"],
                    "required_per_unit": required,
                    "available": available,
                }
            )
            limits.append(available / required)
        unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
        quantum = Decimal(1).scaleb(-unit["decimal_places"])
        available_stock_units = min(limits).quantize(quantum, rounding=ROUND_DOWN)
        return {
            "item_id": item_id,
            "location_id": location_id,
            "available_stock_units": available_stock_units,
            "sellable": available_stock_units > 0,
            "components": components,
        }

    async def credit_exposure(self, customer_id, *, lock=False):
        self.authorize_read()
        customer = await self.catalog.row(
            customer_db.customers, "customer_id", customer_id, lock=lock
        )
        rows = (
            await self.session.execute(
                select(
                    db.lines.c.quantity,
                    db.lines.c.cancelled_quantity,
                    db.lines.c.invoiced_quantity,
                    db.lines.c.line_total,
                )
                .select_from(
                    db.lines.join(
                        db.orders,
                        (db.orders.c.tenant_id == db.lines.c.tenant_id)
                        & (db.orders.c.sales_order_id == db.lines.c.sales_order_id),
                    )
                )
                .where(
                    self.scope(db.lines),
                    db.orders.c.customer_id == customer_id,
                    db.orders.c.confirmed_at.is_not(None),
                )
            )
        ).all()
        committed = sum(
            (
                row.line_total
                * max(ZERO, row.quantity - row.cancelled_quantity - row.invoiced_quantity)
                / row.quantity
                for row in rows
            ),
            ZERO,
        ).quantize(FOUR)
        return {
            "customer_id": customer_id,
            "credit_limit": customer["credit_limit"],
            "committed_uninvoiced": committed,
            "remaining_credit": customer["credit_limit"] - committed,
            "credit_hold": customer["credit_hold"],
        }

    async def action_key(self, key, fingerprint):
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
            if existing["scope"] != "API" or existing["request_fingerprint"] != fingerprint:
                raise ApiError(
                    409, "sales_key_reused", "This action key belongs to another request"
                )
            return existing["response_body"]
        await self.session.execute(
            insert(inventory_db.idempotency_keys).values(
                tenant_id=self.context.tenant_id,
                key=key,
                scope="API",
                request_fingerprint=fingerprint,
            )
        )
        return None

    async def finish_key(self, key, response):
        from app.schemas.sales import SalesOrderOut

        body = SalesOrderOut.model_validate(response).model_dump(mode="json")
        await self.session.execute(
            update(inventory_db.idempotency_keys)
            .where(
                self.scope(inventory_db.idempotency_keys),
                inventory_db.idempotency_keys.c.key == key,
            )
            .values(response_status=200, response_body=body)
        )
        return response

    async def confirm(self, order_id, expected_revision, key):
        self.authorize_write()
        await self.catalog.lock()
        fingerprint = hashlib.sha256(
            f"sales_confirm:{order_id}:{expected_revision}".encode()
        ).hexdigest()
        previous = await self.action_key(key, fingerprint)
        if previous:
            return previous
        order = await self.order(order_id, lock=True)
        if order["status"] != "DRAFT" or order["revision"] != expected_revision:
            raise ApiError(409, "sales_order_stale", "Reload the current sales draft")
        if order["sales_source"] == "POS":
            raise ApiError(
                409,
                "pos_checkout_unavailable",
                "POS checkout requires the billing and payment connection",
            )
        if not order["lines"]:
            raise ApiError(422, "sales_lines_required", "Add at least one sales line")
        customer = await self.catalog.reference(
            customer_db.customers, "customer_id", order["customer_id"]
        )
        exposure = await self.credit_exposure(customer["customer_id"], lock=True)
        if exposure["credit_hold"] or order["grand_total"] > exposure["remaining_credit"]:
            raise ApiError(
                409,
                "customer_credit_exceeded",
                "The customer credit limit or hold blocks confirmation",
            )
        per_line = {}
        totals = defaultdict(lambda: ZERO)
        for line in order["lines"]:
            location_id = line["location_id"] or order["location_id"]
            await self.location(location_id)
            item = await self.catalog.reference(catalog_db.items, "item_id", line["item_id"])
            stock_unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            converted = exact_stock_quantity(
                line["quantity"],
                await self.catalog.factor_for(item, line["uom_id"]),
                stock_unit["decimal_places"],
            )
            if converted != line["quantity_in_stock_uom"]:
                raise ApiError(
                    409, "sales_catalog_changed", "Item conversion changed; review the draft again"
                )
            parts = await self.requirements(line["item_id"], converted)
            per_line[line["sales_order_line_id"]] = (location_id, parts)
            for leaf_id, quantity in parts.items():
                totals[(leaf_id, location_id)] += quantity
        for (leaf_id, location_id), required in sorted(totals.items()):
            leaf = await self.catalog.row(catalog_db.items, "item_id", leaf_id, lock=True)
            balance = (
                (
                    await self.session.execute(
                        select(inventory_db.balances)
                        .where(
                            self.scope(inventory_db.balances),
                            inventory_db.balances.c.item_id == leaf_id,
                            inventory_db.balances.c.location_id == location_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            atp = (balance["physical_on_hand"] - balance["committed"] if balance else ZERO) - leaf[
                "safety_stock"
            ]
            if required > atp:
                raise ApiError(
                    409, "insufficient_stock", f"Insufficient available stock for {leaf['sku']}"
                )
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        for line in order["lines"]:
            location_id, parts = per_line[line["sales_order_line_id"]]
            for leaf_id, quantity in sorted(parts.items()):
                await self.session.execute(
                    insert(db.reservations).values(
                        reservation_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        item_id=leaf_id,
                        location_id=location_id,
                        sales_order_id=order_id,
                        sales_order_line_id=line["sales_order_line_id"],
                        quantity=quantity,
                        state="ACTIVE",
                    )
                )
                await self.posting.append_reservation(
                    order,
                    line["sales_order_line_id"],
                    leaf_id,
                    location_id,
                    quantity,
                    "IN",
                    occurred_at,
                    key,
                )
            await self.session.execute(
                update(db.lines)
                .where(
                    self.scope(db.lines),
                    db.lines.c.sales_order_line_id == line["sales_order_line_id"],
                )
                .values(reserved_quantity=line["quantity"])
            )
        await self.session.execute(
            update(db.orders)
            .where(self.scope(db.orders), db.orders.c.sales_order_id == order_id)
            .values(
                status="CONFIRMED",
                revision=expected_revision + 1,
                confirmed_at=occurred_at,
                credit_check_passed=True,
                credit_limit_at_order=customer["credit_limit"],
            )
        )
        await self.session.execute(
            insert(inventory_db.outbox_events).values(
                outbox_event_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                event_type="inventory.sales_reserved",
                aggregate_id=order_id,
                payload={
                    "sales_order_id": str(order_id),
                    "item_locations": [
                        {"item_id": str(item_id), "location_id": str(location_id)}
                        for item_id, location_id in sorted(totals)
                    ],
                },
                status="PENDING",
            )
        )
        return await self.finish_key(key, await self.order(order_id))

    async def cancel(self, order_id, expected_revision, key, reason):
        self.authorize_write()
        await self.catalog.lock()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "action": "sales_cancel",
                    "order_id": str(order_id),
                    "revision": expected_revision,
                    "reason": reason,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        previous = await self.action_key(key, fingerprint)
        if previous:
            return previous
        order = await self.order(order_id, lock=True)
        if order["revision"] != expected_revision or order["status"] not in {
            "CONFIRMED",
            "PARTIALLY_FULFILLED",
        }:
            raise ApiError(
                409,
                "sales_order_not_cancellable",
                "Reload a confirmed order with unfulfilled stock",
            )
        if not any(
            line["quantity"] > line["shipped_quantity"] + line["cancelled_quantity"]
            for line in order["lines"]
        ):
            raise ApiError(409, "sales_order_fulfilled", "No unfulfilled quantity remains")
        open_shipment = await self.session.scalar(
            select(db.shipments.c.shipment_id).where(
                self.scope(db.shipments),
                db.shipments.c.sales_order_id == order_id,
                db.shipments.c.status.in_(["DRAFT", "PACKED"]),
            )
        )
        if open_shipment:
            raise ApiError(
                409, "shipment_draft_exists", "Cancel the open shipment before cancelling the order"
            )
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        pairs = set()
        for reservation in order["reservations"]:
            if reservation["state"] != "ACTIVE":
                continue
            remaining = reservation["quantity"] - reservation["consumed_quantity"]
            if remaining:
                await self.posting.append_reservation(
                    order,
                    reservation["sales_order_line_id"],
                    reservation["item_id"],
                    reservation["location_id"],
                    remaining,
                    "OUT",
                    occurred_at,
                    key,
                )
                pairs.add((reservation["item_id"], reservation["location_id"]))
            await self.session.execute(
                update(db.reservations)
                .where(
                    self.scope(db.reservations),
                    db.reservations.c.reservation_id == reservation["reservation_id"],
                )
                .values(state="RELEASED", released_at=occurred_at, release_reason=reason)
            )
        for line in order["lines"]:
            remaining = line["quantity"] - line["shipped_quantity"] - line["cancelled_quantity"]
            if remaining:
                await self.session.execute(
                    update(db.lines)
                    .where(
                        self.scope(db.lines),
                        db.lines.c.sales_order_line_id == line["sales_order_line_id"],
                    )
                    .values(
                        reserved_quantity=ZERO,
                        cancelled_quantity=line["cancelled_quantity"] + remaining,
                    )
                )
        await self.session.execute(
            update(db.orders)
            .where(self.scope(db.orders), db.orders.c.sales_order_id == order_id)
            .values(
                status="CANCELLED",
                revision=expected_revision + 1,
                cancelled_at=occurred_at,
                cancel_reason=reason,
            )
        )
        if pairs:
            await self.session.execute(
                insert(inventory_db.outbox_events).values(
                    outbox_event_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    event_type="inventory.sales_released",
                    aggregate_id=order_id,
                    payload={
                        "sales_order_id": str(order_id),
                        "item_locations": [
                            {"item_id": str(item_id), "location_id": str(location_id)}
                            for item_id, location_id in sorted(pairs)
                        ],
                    },
                    status="PENDING",
                )
            )
        return await self.finish_key(key, await self.order(order_id))
