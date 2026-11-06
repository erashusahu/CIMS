"""Phase 3D picking, packing and transactional shipment posting."""

import hashlib
import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as inventory_db
from app.models import procurement as procurement_db
from app.models import sales as db
from app.schemas.sales import ShipmentOut
from app.services.catalog_rules import exact_stock_quantity
from app.services.sales_orders import SalesOrderService
from app.services.valuation import FifoLayer, plan_fifo_issue

ZERO = Decimal(0)
SIX = Decimal("0.000001")


class ShippingService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.sales = SalesOrderService(session, context)
        self.catalog = self.sales.catalog
        self.posting = self.sales.posting

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    async def statuses(self):
        for kind, values in (
            ("SHIPMENT", ("DRAFT", "PACKED", "SHIPPED", "CANCELLED")),
            ("PACKAGE", ("OPEN", "SEALED", "SHIPPED")),
        ):
            for position, code in enumerate(values, 1):
                await self.session.execute(
                    pg_insert(procurement_db.document_statuses)
                    .values(
                        tenant_id=self.context.tenant_id,
                        document_kind=kind,
                        code=code,
                        name=code.title(),
                        sort_order=position,
                        is_initial=position == 1,
                        is_terminal=code in {"SHIPPED", "CANCELLED"},
                        allows_edit=code not in {"SHIPPED", "CANCELLED"},
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id", "document_kind", "code"])
                )

    async def number(self, kind, business_date):
        tenant = await self.catalog.row(catalog_db.tenants, "tenant_id", self.context.tenant_id)
        start = business_date.year - (business_date.month < tenant["fiscal_year_start_month"])
        year = f"{start}-{(start + 1) % 100:02d}"
        prefix = "SHP" if kind == "SHIPMENT" else "PKG"
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

    async def pick_list(self, order_id):
        self.sales.authorize_read()
        order = await self.sales.order(order_id)
        entries = []
        for reservation in order["reservations"]:
            item = await self.catalog.row(catalog_db.items, "item_id", reservation["item_id"])
            location = await self.catalog.row(
                inventory_db.locations, "location_id", reservation["location_id"]
            )
            scanned = await self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(db.pick_scans.c.quantity - db.pick_scans.c.shipped_quantity), 0
                    )
                ).where(
                    self.scope(db.pick_scans),
                    db.pick_scans.c.reservation_id == reservation["reservation_id"],
                )
            )
            remaining = reservation["quantity"] - reservation["consumed_quantity"] - scanned
            entries.append(
                {
                    "reservation_id": reservation["reservation_id"],
                    "sales_order_line_id": reservation["sales_order_line_id"],
                    "item_id": reservation["item_id"],
                    "sku": item["sku"],
                    "barcode": item["barcode"],
                    "location_id": reservation["location_id"],
                    "location_code": location["code"],
                    "reserved_quantity": reservation["quantity"],
                    "shipped_quantity": reservation["consumed_quantity"],
                    "picked_unshipped": scanned,
                    "remaining_to_pick": max(ZERO, remaining),
                }
            )
        return {
            "sales_order_id": order_id,
            "order_no": order["order_no"],
            "status": order["status"],
            "entries": entries,
        }

    async def scans(self, order_id):
        await self.sales.order(order_id)
        rows = (
            await self.session.execute(
                select(db.pick_scans)
                .where(
                    self.scope(db.pick_scans),
                    db.pick_scans.c.sales_order_id == order_id,
                )
                .order_by(db.pick_scans.c.created_at, db.pick_scans.c.pick_scan_id)
            )
        ).mappings()
        return [dict(row) for row in rows]

    async def scan(self, order_id, payload):
        self.sales.authorize_write()
        await self.catalog.lock()
        order = await self.sales.order(order_id, lock=True)
        if order["status"] not in {"CONFIRMED", "PARTIALLY_FULFILLED"}:
            raise ApiError(409, "order_not_pickable", "Only a confirmed order can be picked")
        reservation = next(
            (
                row
                for row in order["reservations"]
                if row["reservation_id"] == payload.reservation_id
            ),
            None,
        )
        if reservation is None or reservation["state"] != "ACTIVE":
            raise ApiError(409, "reservation_not_pickable", "Choose an active reservation")
        item = await self.catalog.row(catalog_db.items, "item_id", reservation["item_id"])
        unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
        exact_stock_quantity(payload.quantity, Decimal(1), unit["decimal_places"])
        code = payload.scanned_code.strip()
        matched = code.casefold() in {item["sku"].casefold(), (item["barcode"] or "").casefold()}
        if not matched and not payload.override_reason:
            raise ApiError(422, "wrong_item_scan", "The scanned code does not match this item")
        if matched and payload.override_reason:
            raise ApiError(422, "unneeded_scan_override", "A matching scan needs no override")
        if not matched and not (self.context.local or self.context.role in {"Owner", "Admin"}):
            raise ApiError(
                403, "scan_override_forbidden", "A manager must approve this scan override"
            )
        total = await self.session.scalar(
            select(func.coalesce(func.sum(db.pick_scans.c.quantity), 0)).where(
                self.scope(db.pick_scans),
                db.pick_scans.c.reservation_id == payload.reservation_id,
            )
        )
        if total + payload.quantity > reservation["quantity"]:
            raise ApiError(
                409, "pick_exceeds_reservation", "The scan exceeds the reserved quantity"
            )
        result = (
            (
                await self.session.execute(
                    insert(db.pick_scans)
                    .values(
                        pick_scan_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        sales_order_id=order_id,
                        sales_order_line_id=reservation["sales_order_line_id"],
                        reservation_id=reservation["reservation_id"],
                        item_id=reservation["item_id"],
                        location_id=reservation["location_id"],
                        scanned_code=code,
                        quantity=payload.quantity,
                        is_override=not matched,
                        override_reason=payload.override_reason if not matched else None,
                        scanned_by=self.context.user_id,
                    )
                    .returning(db.pick_scans)
                )
            )
            .mappings()
            .one()
        )
        return dict(result)

    async def requirements(self, order, line_quantities, location_id):
        lines = {row["sales_order_line_id"]: row for row in order["lines"]}
        reservations = {row["reservation_id"]: row for row in order["reservations"]}
        required = {}
        stock_quantities = {}
        for line_id, quantity in line_quantities.items():
            line = lines.get(line_id)
            if line is None or quantity <= 0:
                raise ApiError(
                    422, "invalid_shipment_line", "Choose a positive order line quantity"
                )
            if line["location_id"] != location_id:
                raise ApiError(
                    422, "shipment_location_mismatch", "Use one source location per shipment"
                )
            if quantity > line["quantity"] - line["shipped_quantity"] - line["cancelled_quantity"]:
                raise ApiError(
                    409, "shipment_exceeds_order", "Shipment quantity exceeds the unshipped order"
                )
            item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"])
            unit = await self.catalog.row(catalog_db.units, "uom_id", line["uom_id"])
            stock_unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            exact_stock_quantity(quantity, Decimal(1), unit["decimal_places"])
            stock_quantities[line_id] = exact_stock_quantity(
                line["quantity_in_stock_uom"] * quantity / line["quantity"],
                Decimal(1),
                stock_unit["decimal_places"],
            )
            parts = [row for row in reservations.values() if row["sales_order_line_id"] == line_id]
            if not parts:
                raise ApiError(
                    409, "shipment_reservation_missing", "The order has no stock snapshot"
                )
            for reservation in parts:
                if reservation["state"] != "ACTIVE" or reservation["location_id"] != location_id:
                    raise ApiError(
                        409, "shipment_reservation_missing", "An active reservation is required"
                    )
                leaf = await self.catalog.row(catalog_db.items, "item_id", reservation["item_id"])
                leaf_unit = await self.catalog.row(catalog_db.units, "uom_id", leaf["stock_uom_id"])
                amount = exact_stock_quantity(
                    reservation["quantity"] * quantity / line["quantity"],
                    Decimal(1),
                    leaf_unit["decimal_places"],
                )
                if amount > reservation["quantity"] - reservation["consumed_quantity"]:
                    raise ApiError(
                        409, "shipment_exceeds_reservation", "Not enough reservation remains"
                    )
                required[reservation["reservation_id"]] = amount
        return required, stock_quantities, reservations, lines

    async def assert_picked(self, required):
        for reservation_id, amount in required.items():
            scanned = await self.session.scalar(
                select(
                    func.coalesce(
                        func.sum(db.pick_scans.c.quantity - db.pick_scans.c.shipped_quantity), 0
                    )
                ).where(
                    self.scope(db.pick_scans),
                    db.pick_scans.c.reservation_id == reservation_id,
                )
            )
            if scanned < amount:
                raise ApiError(409, "shipment_not_picked", "Scan every unit before packing")

    async def shipment(self, shipment_id, *, lock=False):
        shipment = await self.catalog.row(db.shipments, "shipment_id", shipment_id, lock=lock)
        lines = (
            (
                await self.session.execute(
                    select(db.shipment_lines)
                    .where(
                        self.scope(db.shipment_lines),
                        db.shipment_lines.c.shipment_id == shipment_id,
                    )
                    .order_by(db.shipment_lines.c.sales_order_line_id)
                )
            )
            .mappings()
            .all()
        )
        package_ids = (
            (
                await self.session.execute(
                    select(db.shipment_packages.c.package_id).where(
                        self.scope(db.shipment_packages),
                        db.shipment_packages.c.shipment_id == shipment_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        packages = []
        for package_id in package_ids:
            package = await self.catalog.row(db.packages, "package_id", package_id)
            contents = (
                (
                    await self.session.execute(
                        select(db.package_lines).where(
                            self.scope(db.package_lines),
                            db.package_lines.c.package_id == package_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
            packages.append({**package, "contents": [dict(row) for row in contents]})
        return {**shipment, "lines": [dict(row) for row in lines], "packages": packages}

    async def shipments(self, order_id):
        await self.sales.order(order_id)
        ids = (
            (
                await self.session.execute(
                    select(db.shipments.c.shipment_id)
                    .where(
                        self.scope(db.shipments),
                        db.shipments.c.sales_order_id == order_id,
                    )
                    .order_by(db.shipments.c.created_at, db.shipments.c.shipment_id)
                )
            )
            .scalars()
            .all()
        )
        return [await self.shipment(value) for value in ids]

    async def draft(self, order_id, payload):
        self.sales.authorize_write()
        await self.catalog.lock()
        order = await self.sales.order(order_id, lock=True)
        if order["status"] not in {"CONFIRMED", "PARTIALLY_FULFILLED"}:
            raise ApiError(409, "order_not_shippable", "Only a confirmed order can be shipped")
        await self.sales.location(payload.from_location_id)
        existing = await self.session.scalar(
            select(db.shipments.c.shipment_id).where(
                self.scope(db.shipments),
                db.shipments.c.sales_order_id == order_id,
                db.shipments.c.status.in_(["DRAFT", "PACKED"]),
            )
        )
        if existing:
            raise ApiError(409, "shipment_draft_exists", "Review or cancel the open shipment first")
        quantities = {line.sales_order_line_id: line.quantity for line in payload.lines}
        required, stock_quantities, reservations, lines = await self.requirements(
            order,
            quantities,
            payload.from_location_id,
        )
        await self.assert_picked(required)
        packed = defaultdict(lambda: ZERO)
        for package in payload.packages:
            for content in package.contents:
                if content.reservation_id not in required:
                    raise ApiError(
                        422, "package_content_mismatch", "Pack only selected order components"
                    )
                component = reservations[content.reservation_id]
                item = await self.catalog.row(catalog_db.items, "item_id", component["item_id"])
                unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
                exact_stock_quantity(content.quantity, Decimal(1), unit["decimal_places"])
                packed[content.reservation_id] += content.quantity
        if dict(packed) != required:
            raise ApiError(
                422, "package_quantity_mismatch", "Packed contents must match shipment quantities"
            )
        await self.statuses()
        now = await self.session.scalar(select(func.clock_timestamp()))
        shipment_id = uuid.uuid4()
        await self.session.execute(
            insert(db.shipments).values(
                shipment_id=shipment_id,
                tenant_id=self.context.tenant_id,
                shipment_no=await self.number("SHIPMENT", now.date()),
                status="PACKED",
                sales_order_id=order_id,
                from_location_id=payload.from_location_id,
                carrier=payload.carrier,
                tracking_no=payload.tracking_no,
                tracking_url=payload.tracking_url,
                notes=payload.notes,
                is_partial=True,
                created_by=self.context.user_id,
            )
        )
        for line_id, quantity in quantities.items():
            await self.session.execute(
                insert(db.shipment_lines).values(
                    shipment_line_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    shipment_id=shipment_id,
                    sales_order_line_id=line_id,
                    item_id=lines[line_id]["item_id"],
                    quantity=quantity,
                    quantity_in_stock_uom=stock_quantities[line_id],
                )
            )
        for package in payload.packages:
            package_id = uuid.uuid4()
            await self.session.execute(
                insert(db.packages).values(
                    package_id=package_id,
                    tenant_id=self.context.tenant_id,
                    package_no=await self.number("PACKAGE", now.date()),
                    status="SEALED",
                    sales_order_id=order_id,
                    location_id=payload.from_location_id,
                    packed_by=self.context.user_id,
                    packed_at=now,
                    weight_kg=package.weight_kg,
                    length_cm=package.length_cm,
                    width_cm=package.width_cm,
                    height_cm=package.height_cm,
                    carrier=payload.carrier,
                    tracking_no=payload.tracking_no,
                    notes=package.notes,
                )
            )
            await self.session.execute(
                insert(db.shipment_packages).values(
                    tenant_id=self.context.tenant_id,
                    shipment_id=shipment_id,
                    package_id=package_id,
                )
            )
            for content in package.contents:
                reservation = reservations[content.reservation_id]
                await self.session.execute(
                    insert(db.package_lines).values(
                        package_line_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        package_id=package_id,
                        sales_order_line_id=reservation["sales_order_line_id"],
                        item_id=reservation["item_id"],
                        quantity=content.quantity,
                    )
                )
        return await self.shipment(shipment_id)

    async def cancel_draft(self, shipment_id):
        self.sales.authorize_write()
        await self.catalog.lock()
        shipment = await self.shipment(shipment_id, lock=True)
        if shipment["status"] not in {"DRAFT", "PACKED"}:
            raise ApiError(409, "shipment_not_draft", "Only an unshipped document can be cancelled")
        await self.session.execute(
            update(db.shipments)
            .where(
                self.scope(db.shipments),
                db.shipments.c.shipment_id == shipment_id,
            )
            .values(status="CANCELLED")
        )
        return await self.shipment(shipment_id)

    async def fifo_issue(self, item_id, location_id, quantity, physical):
        rows = (
            (
                await self.session.execute(
                    select(inventory_db.fifo_layers)
                    .where(
                        self.scope(inventory_db.fifo_layers),
                        inventory_db.fifo_layers.c.item_id == item_id,
                        inventory_db.fifo_layers.c.location_id == location_id,
                        inventory_db.fifo_layers.c.remaining_qty > 0,
                    )
                    .order_by(
                        inventory_db.fifo_layers.c.received_at,
                        inventory_db.fifo_layers.c.fifo_layer_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .all()
        )
        if sum((row["remaining_qty"] for row in rows), ZERO) != physical:
            raise ApiError(409, "fifo_history_mismatch", "FIFO layers differ from physical stock")
        allocations = plan_fifo_issue(
            [
                FifoLayer(
                    row["fifo_layer_id"], row["received_at"], row["remaining_qty"], row["unit_cost"]
                )
                for row in rows
            ],
            quantity,
        )
        cost = (
            sum((part.quantity * part.unit_cost for part in allocations), ZERO) / quantity
        ).quantize(SIX)
        return allocations, cost

    async def post(self, shipment_id, key):
        self.sales.authorize_write()
        await self.catalog.lock()
        fingerprint = hashlib.sha256(f"ship:{shipment_id}".encode()).hexdigest()
        previous = await self.sales.action_key(key, fingerprint)
        if previous:
            return previous
        shipment = await self.shipment(shipment_id, lock=True)
        if shipment["status"] != "PACKED":
            raise ApiError(409, "shipment_not_packed", "Review a packed shipment before posting")
        order = await self.sales.order(shipment["sales_order_id"], lock=True)
        if order["status"] not in {"CONFIRMED", "PARTIALLY_FULFILLED"}:
            raise ApiError(409, "order_not_shippable", "The order cannot be shipped")
        quantities = {row["sales_order_line_id"]: row["quantity"] for row in shipment["lines"]}
        required, _, reservations, lines = await self.requirements(
            order,
            quantities,
            shipment["from_location_id"],
        )
        await self.assert_picked(required)
        by_pair = {
            (row["sales_order_line_id"], row["item_id"]): row_id
            for row_id, row in reservations.items()
            if row_id in required
        }
        packed = defaultdict(lambda: ZERO)
        for package in shipment["packages"]:
            if (
                package["status"] != "SEALED"
                or package["location_id"] != shipment["from_location_id"]
            ):
                raise ApiError(409, "package_changed", "Review the sealed shipment packages")
            for content in package["contents"]:
                reservation_id = by_pair.get((content["sales_order_line_id"], content["item_id"]))
                if reservation_id is None:
                    raise ApiError(
                        409, "package_changed", "Package contents no longer match the order"
                    )
                packed[reservation_id] += content["quantity"]
        if dict(packed) != required:
            raise ApiError(409, "package_changed", "Packed contents no longer match the shipment")
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        pair_set = set()
        for reservation_id, amount in sorted(required.items()):
            reservation = reservations[reservation_id]
            item_id, location_id = reservation["item_id"], reservation["location_id"]
            item = await self.catalog.row(catalog_db.items, "item_id", item_id, lock=True)
            balance = (
                (
                    await self.session.execute(
                        select(inventory_db.balances)
                        .where(
                            self.scope(inventory_db.balances),
                            inventory_db.balances.c.item_id == item_id,
                            inventory_db.balances.c.location_id == location_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                balance is None
                or balance["physical_on_hand"] < amount
                or balance["committed"] < amount
            ):
                raise ApiError(
                    409, "shipment_stock_changed", "Reserved physical stock is unavailable"
                )
            method = item["valuation_method"] or await self.session.scalar(
                select(catalog_db.tenants.c.default_valuation_method).where(
                    self.scope(catalog_db.tenants)
                )
            )
            allocations, cost = (
                await self.fifo_issue(item_id, location_id, amount, balance["physical_on_hand"])
                if method == "FIFO"
                else ([], balance["avg_unit_cost"])
            )
            source_line = next(
                row
                for row in shipment["lines"]
                if row["sales_order_line_id"] == reservation["sales_order_line_id"]
            )
            await self.posting.append_reservation(
                order,
                reservation["sales_order_line_id"],
                item_id,
                location_id,
                amount,
                "OUT",
                occurred_at,
                key,
                shipment=shipment,
            )
            entry = await self.posting.append_shipment_issue(
                shipment,
                source_line["shipment_line_id"],
                item_id,
                location_id,
                amount,
                cost,
                occurred_at,
                key,
            )
            for allocation in allocations:
                await self.session.execute(
                    update(inventory_db.fifo_layers)
                    .where(
                        self.scope(inventory_db.fifo_layers),
                        inventory_db.fifo_layers.c.fifo_layer_id == allocation.layer_id,
                    )
                    .values(
                        remaining_qty=inventory_db.fifo_layers.c.remaining_qty - allocation.quantity
                    )
                )
                await self.session.execute(
                    insert(inventory_db.fifo_consumptions).values(
                        tenant_id=self.context.tenant_id,
                        fifo_layer_id=allocation.layer_id,
                        ledger_entry_id=entry.entry_id,
                        ledger_entry_at=entry.occurred_at,
                        quantity=allocation.quantity,
                        unit_cost=allocation.unit_cost,
                    )
                )
            new_consumed = reservation["consumed_quantity"] + amount
            await self.session.execute(
                update(db.reservations)
                .where(
                    self.scope(db.reservations),
                    db.reservations.c.reservation_id == reservation_id,
                )
                .values(
                    consumed_quantity=new_consumed,
                    state="CONSUMED" if new_consumed == reservation["quantity"] else "ACTIVE",
                )
            )
            remaining = amount
            scans = (
                (
                    await self.session.execute(
                        select(db.pick_scans)
                        .where(
                            self.scope(db.pick_scans),
                            db.pick_scans.c.reservation_id == reservation_id,
                            db.pick_scans.c.quantity > db.pick_scans.c.shipped_quantity,
                        )
                        .order_by(db.pick_scans.c.created_at, db.pick_scans.c.pick_scan_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            for scan in scans:
                take = min(remaining, scan["quantity"] - scan["shipped_quantity"])
                if take:
                    await self.session.execute(
                        update(db.pick_scans)
                        .where(
                            self.scope(db.pick_scans),
                            db.pick_scans.c.pick_scan_id == scan["pick_scan_id"],
                        )
                        .values(shipped_quantity=scan["shipped_quantity"] + take)
                    )
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                raise ApiError(409, "shipment_not_picked", "Pick quantities changed")
            pair_set.add((item_id, location_id))
        complete = True
        for line_id, quantity in quantities.items():
            line = lines[line_id]
            shipped = line["shipped_quantity"] + quantity
            await self.session.execute(
                update(db.lines)
                .where(
                    self.scope(db.lines),
                    db.lines.c.sales_order_line_id == line_id,
                )
                .values(
                    shipped_quantity=shipped, reserved_quantity=line["reserved_quantity"] - quantity
                )
            )
        for line in order["lines"]:
            if (
                line["shipped_quantity"]
                + quantities.get(line["sales_order_line_id"], ZERO)
                + line["cancelled_quantity"]
                < line["quantity"]
            ):
                complete = False
        await self.session.execute(
            update(db.orders)
            .where(
                self.scope(db.orders),
                db.orders.c.sales_order_id == order["sales_order_id"],
            )
            .values(
                status="FULFILLED" if complete else "PARTIALLY_FULFILLED",
                revision=order["revision"] + 1,
            )
        )
        for package in shipment["packages"]:
            await self.session.execute(
                update(db.packages)
                .where(
                    self.scope(db.packages),
                    db.packages.c.package_id == package["package_id"],
                )
                .values(status="SHIPPED")
            )
        await self.session.execute(
            update(db.shipments)
            .where(
                self.scope(db.shipments),
                db.shipments.c.shipment_id == shipment_id,
            )
            .values(status="SHIPPED", shipped_at=occurred_at, is_partial=not complete)
        )
        await self.session.execute(
            insert(inventory_db.outbox_events).values(
                outbox_event_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                event_type="inventory.sales_shipped",
                aggregate_id=shipment_id,
                payload={
                    "shipment_id": str(shipment_id),
                    "item_locations": [
                        {"item_id": str(item_id), "location_id": str(location_id)}
                        for item_id, location_id in sorted(pair_set)
                    ],
                },
                status="PENDING",
            )
        )
        result = await self.shipment(shipment_id)
        body = ShipmentOut.model_validate(result).model_dump(mode="json")
        await self.session.execute(
            update(inventory_db.idempotency_keys)
            .where(
                self.scope(inventory_db.idempotency_keys),
                inventory_db.idempotency_keys.c.key == key,
            )
            .values(response_status=200, response_body=body)
        )
        return result
