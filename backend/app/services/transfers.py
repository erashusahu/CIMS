"""Phase 3E transfer workflow. All stock effects are ledger inserts in one transaction."""

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as stock
from app.models import procurement as procurement_db
from app.models import transfers as db
from app.schemas.transfers import TransferOut
from app.services.catalog import CatalogService
from app.services.catalog_rules import exact_stock_quantity
from app.services.inventory import InventoryService
from app.services.valuation import FifoLayer, plan_fifo_issue

ZERO = Decimal(0)
SIX = Decimal("0.000001")


class TransferService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    async def prepare(self):
        await InventoryService(self.session, self.context).prepare()

    def authorize_write(self):
        if not self.context.can_write:
            raise ApiError(
                403, "transfer_write_forbidden", "Only an Owner or Admin can change transfers"
            )

    async def statuses(self):
        values = ("DRAFT", "CONFIRMED", "IN_TRANSIT", "PARTIALLY_RECEIVED", "RECEIVED", "SETTLED")
        for position, code in enumerate(values, 1):
            await self.session.execute(
                pg_insert(procurement_db.document_statuses)
                .values(
                    tenant_id=self.context.tenant_id,
                    document_kind="TRANSFER_ORDER",
                    code=code,
                    name=code.replace("_", " ").title(),
                    sort_order=position,
                    is_initial=code == "DRAFT",
                    is_terminal=code in {"RECEIVED", "SETTLED"},
                    allows_edit=code == "DRAFT",
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "document_kind", "code"])
            )

    async def number(self):
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
                        document_kind="TRANSFER_ORDER",
                        fiscal_year=year,
                        prefix="TRF",
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

    async def location(self, location_id, *, lock=False):
        row = await self.catalog.row(stock.locations, "location_id", location_id, lock=lock)
        kind = await self.catalog.row(
            stock.location_types, "location_type_id", row["location_type_id"]
        )
        if (
            not row["is_active"]
            or not kind["is_storage"]
            or kind["is_in_transit"]
            or kind["code"].upper() not in {"WAREHOUSE", "GODOWN", "STORE"}
        ):
            raise ApiError(422, "transfer_location_invalid", "Choose an active storage location")
        return row

    async def same_registration(self, source, destination):
        source_reg = source["tax_registration_id"]
        dest_reg = destination["tax_registration_id"]
        if source_reg is None or source_reg != dest_reg:
            raise ApiError(
                409,
                "transfer_gst_registration",
                "Map both locations to the same GST registration before transferring",
            )

    async def registrations(self):
        return (
            (
                await self.session.execute(
                    select(db.registrations)
                    .where(self.scope(db.registrations))
                    .order_by(db.registrations.c.gstin)
                )
            )
            .mappings()
            .all()
        )

    async def create_registration(self, payload):
        self.authorize_write()
        await self.catalog.lock()
        result = (
            (
                await self.session.execute(
                    insert(db.registrations)
                    .values(
                        tax_registration_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        gstin=payload.gstin,
                        legal_name=payload.legal_name,
                        state_code=payload.state_code,
                        registration_type="REGULAR",
                        is_primary=False,
                    )
                    .returning(db.registrations)
                )
            )
            .mappings()
            .one()
        )
        return dict(result)

    async def locations(self):
        return (
            (
                await self.session.execute(
                    select(stock.locations)
                    .join(
                        stock.location_types,
                        (stock.location_types.c.tenant_id == stock.locations.c.tenant_id)
                        & (
                            stock.location_types.c.location_type_id
                            == stock.locations.c.location_type_id
                        ),
                    )
                    .where(
                        self.scope(stock.locations),
                        stock.locations.c.is_active.is_(True),
                        stock.location_types.c.is_storage.is_(True),
                        stock.location_types.c.is_in_transit.is_(False),
                    )
                    .order_by(stock.locations.c.code)
                )
            )
            .mappings()
            .all()
        )

    async def assign_registration(self, location_id, registration_id):
        self.authorize_write()
        await self.catalog.lock()
        location = await self.location(location_id, lock=True)
        registration = await self.catalog.row(
            db.registrations, "tax_registration_id", registration_id
        )
        if location["state_code"] not in {None, registration["state_code"]}:
            raise ApiError(
                422,
                "registration_state_mismatch",
                "The location and GST registration state codes differ",
            )
        if location["tax_registration_id"] not in {None, registration_id}:
            used = await self.session.scalar(
                select(func.count())
                .select_from(stock.ledger)
                .where(self.scope(stock.ledger), stock.ledger.c.location_id == location_id)
            )
            if used:
                raise ApiError(
                    409,
                    "registration_locked",
                    "A location with stock history cannot change GST registration",
                )
        await self.session.execute(
            update(stock.locations)
            .where(self.scope(stock.locations), stock.locations.c.location_id == location_id)
            .values(
                tax_registration_id=registration_id,
                state_code=registration["state_code"]
                if location["state_code"] is None
                else location["state_code"],
            )
        )
        return await self.catalog.row(stock.locations, "location_id", location_id)

    async def document(self, order_id, *, lock=False):
        row = await self.catalog.row(db.orders, "transfer_order_id", order_id, lock=lock)
        lines = (
            (
                await self.session.execute(
                    select(db.lines)
                    .where(self.scope(db.lines), db.lines.c.transfer_order_id == order_id)
                    .order_by(db.lines.c.line_no)
                )
            )
            .mappings()
            .all()
        )
        operations = (
            (
                await self.session.execute(
                    select(db.operations)
                    .where(self.scope(db.operations), db.operations.c.transfer_order_id == order_id)
                    .order_by(db.operations.c.created_at, db.operations.c.transfer_operation_id)
                )
            )
            .mappings()
            .all()
        )
        return {
            **row,
            "lines": [dict(line) for line in lines],
            "operations": [dict(op) for op in operations],
        }

    async def list(self):
        ids = (
            (
                await self.session.execute(
                    select(db.orders.c.transfer_order_id)
                    .where(self.scope(db.orders))
                    .order_by(db.orders.c.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [await self.document(order_id) for order_id in ids]

    async def in_transit(self):
        outstanding = (
            db.lines.c.dispatched_quantity
            - db.lines.c.received_quantity
            - db.lines.c.short_quantity
            - db.lines.c.damaged_quantity
        )
        return (
            (
                await self.session.execute(
                    select(
                        db.orders.c.transfer_order_id,
                        db.orders.c.transfer_no,
                        db.orders.c.from_location_id,
                        db.orders.c.to_location_id,
                        db.orders.c.in_transit_location_id,
                        db.lines.c.transfer_order_line_id,
                        db.lines.c.item_id,
                        outstanding.label("outstanding_quantity"),
                    )
                    .join(
                        db.lines,
                        (db.lines.c.tenant_id == db.orders.c.tenant_id)
                        & (db.lines.c.transfer_order_id == db.orders.c.transfer_order_id),
                    )
                    .where(
                        self.scope(db.orders),
                        db.orders.c.status.in_(["IN_TRANSIT", "PARTIALLY_RECEIVED"]),
                        outstanding > 0,
                    )
                    .order_by(db.orders.c.created_at, db.lines.c.line_no)
                )
            )
            .mappings()
            .all()
        )

    async def create(self, payload):
        self.authorize_write()
        await self.catalog.lock()
        source = await self.location(payload.from_location_id)
        destination = await self.location(payload.to_location_id)
        await self.same_registration(source, destination)
        items = []
        for line in payload.lines:
            item = await self.catalog.reference(catalog_db.items, "item_id", line.item_id)
            item_type = await self.catalog.row(
                catalog_db.item_types, "item_type_id", item["item_type_id"]
            )
            if item["item_kind"] not in {"STANDALONE", "VARIANT"} or not item_type["is_stockable"]:
                raise ApiError(
                    422, "transfer_item_invalid", "Choose a stock-tracked product or variant"
                )
            if any(
                item[field]
                for field in ("track_batch", "track_lot", "track_serial", "track_expiry")
            ):
                raise ApiError(
                    422, "tracked_transfer_pending", "Tracked items need batch or serial details"
                )
            unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
            quantity = exact_stock_quantity(line.quantity, Decimal(1), unit["decimal_places"])
            items.append((item, quantity))
        await self.statuses()
        order_id = uuid.uuid4()
        number = await self.number()
        # A dedicated system location keeps each transfer's costs and FIFO layers separate.
        await self.session.execute(
            pg_insert(stock.location_types)
            .values(
                location_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code="IN_TRANSIT",
                name="In transit",
                is_in_transit=True,
                is_storage=False,
                is_system=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        transit_type = (
            (
                await self.session.execute(
                    select(stock.location_types).where(
                        self.scope(stock.location_types),
                        stock.location_types.c.code == "IN_TRANSIT",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            not transit_type
            or not transit_type["is_in_transit"]
            or transit_type["is_storage"]
            or not transit_type["is_system"]
        ):
            raise ApiError(409, "transit_type_invalid", "The transit location type requires review")
        transit_id = uuid.uuid4()
        await self.session.execute(
            insert(stock.locations).values(
                location_id=transit_id,
                tenant_id=self.context.tenant_id,
                location_type_id=transit_type["location_type_id"],
                code=f"TRN-{order_id.hex[:12]}",
                name=f"In transit · {number}",
                tax_registration_id=source["tax_registration_id"],
                state_code=source["state_code"],
                is_active=True,
            )
        )
        await self.session.execute(
            insert(db.orders).values(
                transfer_order_id=order_id,
                tenant_id=self.context.tenant_id,
                transfer_no=number,
                status="DRAFT",
                from_location_id=source["location_id"],
                to_location_id=destination["location_id"],
                in_transit_location_id=transit_id,
                expected_arrival_date=payload.expected_arrival_date,
                notes=payload.notes,
                requested_by=self.context.user_id,
                created_by=self.context.user_id,
            )
        )
        for position, (item, quantity) in enumerate(items, 1):
            await self.session.execute(
                insert(db.lines).values(
                    transfer_order_line_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    transfer_order_id=order_id,
                    line_no=position,
                    item_id=item["item_id"],
                    uom_id=item["stock_uom_id"],
                    requested_quantity=quantity,
                    quantity_in_stock_uom=quantity,
                )
            )
        return await self.document(order_id)

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
            if prior["request_fingerprint"] != fingerprint or prior["scope"] != "POSTING":
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

    async def finish(self, key, document):
        body = TransferOut.model_validate(document).model_dump(mode="json")
        await self.session.execute(
            update(stock.idempotency_keys)
            .where(self.scope(stock.idempotency_keys), stock.idempotency_keys.c.key == key)
            .values(response_status=200, response_body=body)
        )
        return document

    async def movement(self, code, state, direction):
        await self.session.execute(
            pg_insert(stock.movement_types)
            .values(
                movement_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code=code,
                name=code.replace("_", " ").title(),
                stock_state=state,
                direction_hint=direction,
                requires_reason=False,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        row = (
            (
                await self.session.execute(
                    select(stock.movement_types).where(
                        self.scope(stock.movement_types), stock.movement_types.c.code == code
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            row["stock_state"] != state
            or row["direction_hint"] != direction
            or row["requires_reason"]
            or not row["is_active"]
        ):
            raise ApiError(
                409,
                "transfer_movement_configuration",
                "Transfer movement configuration requires review",
            )
        return row["movement_type_id"]

    async def post_ledger(
        self, document, line, location_id, state, direction, quantity, cost, code, key, occurred_at
    ):
        movement_id = await self.movement(code, state, direction)
        return (
            await self.session.execute(
                insert(stock.ledger)
                .values(
                    tenant_id=self.context.tenant_id,
                    occurred_at=occurred_at,
                    item_id=line["item_id"],
                    location_id=location_id,
                    stock_state=state,
                    movement_type_id=movement_id,
                    direction=direction,
                    quantity=quantity,
                    unit_cost=cost,
                    source_document_type="TRANSFER_ORDER",
                    source_document_id=document["transfer_order_id"],
                    source_document_line_id=line["transfer_order_line_id"],
                    source_document_no=document["transfer_no"],
                    idempotency_key=key,
                    created_by=self.context.user_id,
                )
                .returning(stock.ledger.c.entry_id, stock.ledger.c.occurred_at)
            )
        ).one()

    async def event(self, document, pairs, action):
        await self.session.execute(
            insert(stock.outbox_events).values(
                outbox_event_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                event_type=f"inventory.transfer_{action}",
                aggregate_id=document["transfer_order_id"],
                payload={
                    "transfer_order_id": str(document["transfer_order_id"]),
                    "item_locations": [
                        {"item_id": str(item), "location_id": str(loc)}
                        for item, loc in sorted(pairs)
                    ],
                },
                status="PENDING",
            )
        )

    async def confirm(self, order_id, key):
        self.authorize_write()
        await self.catalog.lock()
        previous = await self.key(key, "transfer.confirm", {"order": str(order_id)})
        if previous:
            return previous
        doc = await self.document(order_id, lock=True)
        if doc["status"] != "DRAFT":
            raise ApiError(409, "transfer_not_draft", "Only a draft transfer can be confirmed")
        source = await self.location(doc["from_location_id"], lock=True)
        destination = await self.location(doc["to_location_id"], lock=True)
        await self.same_registration(source, destination)
        pairs = set()
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        for line in sorted(doc["lines"], key=lambda value: str(value["item_id"])):
            item = await self.catalog.reference(catalog_db.items, "item_id", line["item_id"])
            balance = (
                (
                    await self.session.execute(
                        select(stock.balances)
                        .where(
                            self.scope(stock.balances),
                            stock.balances.c.item_id == line["item_id"],
                            stock.balances.c.location_id == source["location_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            available = (
                balance["physical_on_hand"] - balance["committed"] if balance else ZERO
            ) - item["safety_stock"]
            if available < line["quantity_in_stock_uom"]:
                raise ApiError(
                    409,
                    "insufficient_transfer_stock",
                    f"Insufficient available stock for {item['sku']}",
                )
            quantity = line["quantity_in_stock_uom"]
            await self.post_ledger(
                doc,
                line,
                source["location_id"],
                "COMMITTED",
                "IN",
                quantity,
                None,
                "TRANSFER_RESERVE",
                key,
                occurred_at,
            )
            await self.post_ledger(
                doc,
                line,
                destination["location_id"],
                "INCOMING",
                "IN",
                quantity,
                None,
                "TRANSFER_EXPECTED",
                key,
                occurred_at,
            )
            pairs.update(
                {
                    (line["item_id"], source["location_id"]),
                    (line["item_id"], destination["location_id"]),
                }
            )
        await self.session.execute(
            update(db.orders)
            .where(self.scope(db.orders), db.orders.c.transfer_order_id == order_id)
            .values(status="CONFIRMED")
        )
        await self.event(doc, pairs, "confirmed")
        return await self.finish(key, await self.document(order_id))

    async def dispatch(self, order_id, key):
        self.authorize_write()
        await self.catalog.lock()
        previous = await self.key(key, "transfer.dispatch", {"order": str(order_id)})
        if previous:
            return previous
        doc = await self.document(order_id, lock=True)
        if doc["status"] != "CONFIRMED":
            raise ApiError(409, "transfer_not_confirmed", "Confirm the transfer before dispatch")
        source = await self.location(doc["from_location_id"], lock=True)
        destination = await self.location(doc["to_location_id"], lock=True)
        await self.same_registration(source, destination)
        pairs = set()
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        for line in sorted(doc["lines"], key=lambda value: str(value["item_id"])):
            quantity = line["quantity_in_stock_uom"]
            item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"], lock=True)
            balance = (
                (
                    await self.session.execute(
                        select(stock.balances)
                        .where(
                            self.scope(stock.balances),
                            stock.balances.c.item_id == line["item_id"],
                            stock.balances.c.location_id == source["location_id"],
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                not balance
                or balance["physical_on_hand"] < quantity
                or balance["committed"] < quantity
            ):
                raise ApiError(
                    409,
                    "transfer_stock_changed",
                    "Source stock or reservation changed before dispatch",
                )
            method = item["valuation_method"] or await self.session.scalar(
                select(catalog_db.tenants.c.default_valuation_method).where(
                    self.scope(catalog_db.tenants)
                )
            )
            if method == "FIFO":
                source_layers = (
                    (
                        await self.session.execute(
                            select(stock.fifo_layers)
                            .where(
                                self.scope(stock.fifo_layers),
                                stock.fifo_layers.c.item_id == line["item_id"],
                                stock.fifo_layers.c.location_id == source["location_id"],
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
                if (
                    sum((row["remaining_qty"] for row in source_layers), ZERO)
                    != balance["physical_on_hand"]
                ):
                    raise ApiError(
                        409, "fifo_history_mismatch", "FIFO layers differ from source stock"
                    )
                allocations = plan_fifo_issue(
                    [
                        FifoLayer(
                            row["fifo_layer_id"],
                            row["received_at"],
                            row["remaining_qty"],
                            row["unit_cost"],
                        )
                        for row in source_layers
                    ],
                    quantity,
                )
                parts = [(part.layer_id, part.quantity, part.unit_cost) for part in allocations]
            else:
                parts = [(None, quantity, balance["avg_unit_cost"])]
            await self.post_ledger(
                doc,
                line,
                source["location_id"],
                "COMMITTED",
                "OUT",
                quantity,
                None,
                "TRANSFER_RELEASE",
                key,
                occurred_at,
            )
            for source_layer_id, amount, cost in parts:
                source_entry = await self.post_ledger(
                    doc,
                    line,
                    source["location_id"],
                    "PHYSICAL",
                    "OUT",
                    amount,
                    cost,
                    "TRANSFER_DISPATCH",
                    key,
                    occurred_at,
                )
                transit_entry = await self.post_ledger(
                    doc,
                    line,
                    doc["in_transit_location_id"],
                    "PHYSICAL",
                    "IN",
                    amount,
                    cost,
                    "TRANSFER_TRANSIT_IN",
                    key,
                    occurred_at,
                )
                transit_layer_id = None
                if source_layer_id is not None:
                    await self.session.execute(
                        update(stock.fifo_layers)
                        .where(
                            self.scope(stock.fifo_layers),
                            stock.fifo_layers.c.fifo_layer_id == source_layer_id,
                        )
                        .values(remaining_qty=stock.fifo_layers.c.remaining_qty - amount)
                    )
                    await self.session.execute(
                        insert(stock.fifo_consumptions).values(
                            tenant_id=self.context.tenant_id,
                            fifo_layer_id=source_layer_id,
                            ledger_entry_id=source_entry.entry_id,
                            ledger_entry_at=source_entry.occurred_at,
                            quantity=amount,
                            unit_cost=cost,
                        )
                    )
                    transit_layer_id = (
                        await self.session.execute(
                            insert(stock.fifo_layers)
                            .values(
                                tenant_id=self.context.tenant_id,
                                item_id=line["item_id"],
                                location_id=doc["in_transit_location_id"],
                                received_at=occurred_at,
                                original_qty=amount,
                                remaining_qty=amount,
                                unit_cost=cost,
                                source_ledger_entry_id=transit_entry.entry_id,
                                source_ledger_entry_at=transit_entry.occurred_at,
                            )
                            .returning(stock.fifo_layers.c.fifo_layer_id)
                        )
                    ).scalar_one()
                await self.session.execute(
                    insert(db.operations).values(
                        transfer_operation_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        transfer_order_id=order_id,
                        transfer_order_line_id=line["transfer_order_line_id"],
                        action="DISPATCH",
                        quantity=amount,
                        unit_cost=cost,
                        source_fifo_layer_id=source_layer_id,
                        transit_fifo_layer_id=transit_layer_id,
                        ledger_entry_id=source_entry.entry_id,
                        ledger_entry_at=source_entry.occurred_at,
                        created_by=self.context.user_id,
                    )
                )
            await self.session.execute(
                update(db.lines)
                .where(
                    self.scope(db.lines),
                    db.lines.c.transfer_order_line_id == line["transfer_order_line_id"],
                )
                .values(dispatched_quantity=quantity)
            )
            pairs.update(
                {
                    (line["item_id"], source["location_id"]),
                    (line["item_id"], doc["in_transit_location_id"]),
                }
            )
        await self.session.execute(
            update(db.orders)
            .where(self.scope(db.orders), db.orders.c.transfer_order_id == order_id)
            .values(
                status="IN_TRANSIT", dispatch_at=occurred_at, dispatched_by=self.context.user_id
            )
        )
        await self.event(doc, pairs, "dispatched")
        return await self.finish(key, await self.document(order_id))

    async def resolve(self, order_id, payload, *, loss=False):
        self.authorize_write()
        await self.catalog.lock()
        action = "loss" if loss else "receive"
        previous = await self.key(
            payload.idempotency_key,
            f"transfer.{action}",
            {
                "order": str(order_id),
                "line": str(payload.line_id),
                "quantity": str(payload.quantity),
                "reason": payload.reason if loss else None,
            },
        )
        if previous:
            return previous
        doc = await self.document(order_id, lock=True)
        if doc["status"] not in {"IN_TRANSIT", "PARTIALLY_RECEIVED"}:
            raise ApiError(
                409,
                "transfer_not_in_transit",
                "Dispatch the transfer before recording a receipt or loss",
            )
        line = next(
            (line for line in doc["lines"] if line["transfer_order_line_id"] == payload.line_id),
            None,
        )
        if line is None:
            raise ApiError(404, "transfer_line_missing", "The transfer line is unavailable")
        item = await self.catalog.row(catalog_db.items, "item_id", line["item_id"])
        unit = await self.catalog.row(catalog_db.units, "uom_id", item["stock_uom_id"])
        quantity = exact_stock_quantity(payload.quantity, Decimal(1), unit["decimal_places"])
        outstanding = (
            line["dispatched_quantity"]
            - line["received_quantity"]
            - line["short_quantity"]
            - line["damaged_quantity"]
        )
        if quantity > outstanding:
            raise ApiError(
                409, "transfer_exceeds_transit", "Quantity exceeds the outstanding goods in transit"
            )
        source = await self.location(doc["from_location_id"])
        destination = await self.location(doc["to_location_id"], lock=True)
        await self.same_registration(source, destination)
        dispatches = (
            (
                await self.session.execute(
                    select(db.operations)
                    .where(
                        self.scope(db.operations),
                        db.operations.c.transfer_order_line_id == payload.line_id,
                        db.operations.c.action == "DISPATCH",
                    )
                    .order_by(db.operations.c.created_at, db.operations.c.transfer_operation_id)
                )
            )
            .mappings()
            .all()
        )
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        remaining = quantity
        for dispatch in dispatches:
            used = await self.session.scalar(
                select(func.coalesce(func.sum(db.operations.c.quantity), 0)).where(
                    self.scope(db.operations),
                    db.operations.c.source_operation_id == dispatch["transfer_operation_id"],
                )
            )
            take = min(remaining, dispatch["quantity"] - used)
            if not take:
                continue
            transit_entry = await self.post_ledger(
                doc,
                line,
                doc["in_transit_location_id"],
                "PHYSICAL",
                "OUT",
                take,
                dispatch["unit_cost"],
                "TRANSFER_LOSS" if loss else "TRANSFER_TRANSIT_OUT",
                payload.idempotency_key,
                occurred_at,
            )
            destination_entry = None
            destination_layer_id = None
            if not loss:
                destination_entry = await self.post_ledger(
                    doc,
                    line,
                    destination["location_id"],
                    "PHYSICAL",
                    "IN",
                    take,
                    dispatch["unit_cost"],
                    "TRANSFER_RECEIVE",
                    payload.idempotency_key,
                    occurred_at,
                )
            if dispatch["transit_fifo_layer_id"] is not None:
                await self.session.execute(
                    update(stock.fifo_layers)
                    .where(
                        self.scope(stock.fifo_layers),
                        stock.fifo_layers.c.fifo_layer_id == dispatch["transit_fifo_layer_id"],
                    )
                    .values(remaining_qty=stock.fifo_layers.c.remaining_qty - take)
                )
                await self.session.execute(
                    insert(stock.fifo_consumptions).values(
                        tenant_id=self.context.tenant_id,
                        fifo_layer_id=dispatch["transit_fifo_layer_id"],
                        ledger_entry_id=transit_entry.entry_id,
                        ledger_entry_at=transit_entry.occurred_at,
                        quantity=take,
                        unit_cost=dispatch["unit_cost"],
                    )
                )
                if destination_entry is not None:
                    destination_layer_id = (
                        await self.session.execute(
                            insert(stock.fifo_layers)
                            .values(
                                tenant_id=self.context.tenant_id,
                                item_id=line["item_id"],
                                location_id=destination["location_id"],
                                received_at=occurred_at,
                                original_qty=take,
                                remaining_qty=take,
                                unit_cost=dispatch["unit_cost"],
                                source_ledger_entry_id=destination_entry.entry_id,
                                source_ledger_entry_at=destination_entry.occurred_at,
                            )
                            .returning(stock.fifo_layers.c.fifo_layer_id)
                        )
                    ).scalar_one()
            await self.session.execute(
                insert(db.operations).values(
                    transfer_operation_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    transfer_order_id=order_id,
                    transfer_order_line_id=payload.line_id,
                    action="LOSS" if loss else "RECEIVE",
                    quantity=take,
                    unit_cost=dispatch["unit_cost"],
                    source_fifo_layer_id=dispatch["source_fifo_layer_id"],
                    transit_fifo_layer_id=dispatch["transit_fifo_layer_id"],
                    destination_fifo_layer_id=destination_layer_id,
                    source_operation_id=dispatch["transfer_operation_id"],
                    ledger_entry_id=transit_entry.entry_id,
                    ledger_entry_at=transit_entry.occurred_at,
                    reason=payload.reason if loss else None,
                    approved_by=self.context.user_id if loss else None,
                    created_by=self.context.user_id,
                )
            )
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise ApiError(
                409, "transfer_provenance_mismatch", "Transfer movement history requires review"
            )
        await self.post_ledger(
            doc,
            line,
            destination["location_id"],
            "INCOMING",
            "OUT",
            quantity,
            None,
            "TRANSFER_ARRIVED" if not loss else "TRANSFER_EXPECTED_LOSS",
            payload.idempotency_key,
            occurred_at,
        )
        await self.session.execute(
            update(db.lines)
            .where(self.scope(db.lines), db.lines.c.transfer_order_line_id == payload.line_id)
            .values(
                short_quantity=line["short_quantity"] + quantity
                if loss
                else line["short_quantity"],
                received_quantity=line["received_quantity"] + quantity
                if not loss
                else line["received_quantity"],
            )
        )
        if loss:
            await self.session.execute(
                update(db.orders)
                .where(self.scope(db.orders), db.orders.c.transfer_order_id == order_id)
                .values(has_variance=True)
            )
        doc = await self.document(order_id)
        complete = all(
            row["dispatched_quantity"]
            == row["received_quantity"] + row["short_quantity"] + row["damaged_quantity"]
            for row in doc["lines"]
        )
        status = (
            ("SETTLED" if doc["has_variance"] else "RECEIVED") if complete else "PARTIALLY_RECEIVED"
        )
        await self.session.execute(
            update(db.orders)
            .where(self.scope(db.orders), db.orders.c.transfer_order_id == order_id)
            .values(
                status=status,
                received_at=occurred_at if complete else None,
                received_by=self.context.user_id if complete else None,
            )
        )
        await self.event(
            doc,
            {
                (line["item_id"], doc["in_transit_location_id"]),
                (line["item_id"], destination["location_id"]),
            },
            action,
        )
        return await self.finish(payload.idempotency_key, await self.document(order_id))
