import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

from sqlalchemy import delete, exists, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext
from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as db
from app.schemas.inventory import LocationIn, OpeningIn, OpeningOut, OpeningUpdate
from app.services.catalog import CatalogService
from app.services.catalog_rules import exact_stock_quantity


class InventoryPostingService:
    """The sole application owner of stock-ledger INSERTs. Never edits balance quantities."""

    def __init__(self, session: AsyncSession, context: CatalogContext):
        self.session, self.context = session, context

    async def opening_movement_type(self, state: str):
        code = f"OPENING_{state}"
        await self.session.execute(
            pg_insert(db.movement_types)
            .values(
                movement_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code=code,
                name=f"Opening {state.lower()} stock",
                stock_state=state,
                direction_hint="IN",
                requires_reason=False,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        row = (
            (
                await self.session.execute(
                    select(db.movement_types).where(
                        db.movement_types.c.tenant_id == self.context.tenant_id,
                        db.movement_types.c.code == code,
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            row["stock_state"] != state
            or row["direction_hint"] != "IN"
            or not row["is_active"]
            or row["requires_reason"]
        ):
            raise ApiError(
                409,
                "opening_movement_configuration",
                "The opening movement configuration requires review",
            )
        return row["movement_type_id"]

    async def append_opening(
        self, document: dict, line: dict, method: str, occurred_at: datetime, key: str
    ):
        # The existing BEFORE INSERT trigger updates physical/accounting quantities and
        # weighted-average cost. Do not perform a second balance UPDATE here.
        for state in ["PHYSICAL", "ACCOUNTING"]:
            movement_type = await self.opening_movement_type(state)
            entry = (
                await self.session.execute(
                    insert(db.ledger)
                    .values(
                        tenant_id=self.context.tenant_id,
                        occurred_at=occurred_at,
                        item_id=line["item_id"],
                        location_id=line["location_id"],
                        stock_state=state,
                        movement_type_id=movement_type,
                        direction="IN",
                        quantity=line["stock_quantity"],
                        unit_cost=line["unit_cost"],
                        source_document_type="OPENING_STOCK",
                        source_document_id=document["opening_stock_id"],
                        source_document_line_id=line["opening_stock_line_id"],
                        source_document_no=document["document_no"],
                        idempotency_key=key,
                        created_by=self.context.user_id,
                    )
                    .returning(db.ledger.c.entry_id, db.ledger.c.occurred_at)
                )
            ).one()
            if state == "PHYSICAL" and method == "FIFO":
                await self.session.execute(
                    insert(db.fifo_layers).values(
                        tenant_id=self.context.tenant_id,
                        item_id=line["item_id"],
                        location_id=line["location_id"],
                        received_at=occurred_at,
                        original_qty=line["stock_quantity"],
                        remaining_qty=line["stock_quantity"],
                        unit_cost=line["unit_cost"],
                        source_ledger_entry_id=entry.entry_id,
                        source_ledger_entry_at=entry.occurred_at,
                    )
                )

    async def append_purchase_receipt(
        self, document, line, location_id, quantity, unit_cost, method, occurred_at, key
    ):
        """Post physical stock only; billing owns any later accounting movement."""
        movement_id = await self.session.scalar(
            select(db.movement_types.c.movement_type_id).where(
                db.movement_types.c.tenant_id == self.context.tenant_id,
                db.movement_types.c.code == "PURCHASE_RECEIPT",
                db.movement_types.c.stock_state == "PHYSICAL",
                db.movement_types.c.is_active.is_(True),
            )
        )
        if movement_id is None:
            await self.session.execute(
                pg_insert(db.movement_types)
                .values(
                    movement_type_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    code="PURCHASE_RECEIPT",
                    name="Purchase receipt",
                    stock_state="PHYSICAL",
                    direction_hint="IN",
                    requires_reason=False,
                    is_system=True,
                    is_active=True,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
            )
            movement_id = await self.session.scalar(
                select(db.movement_types.c.movement_type_id).where(
                    db.movement_types.c.tenant_id == self.context.tenant_id,
                    db.movement_types.c.code == "PURCHASE_RECEIPT",
                    db.movement_types.c.stock_state == "PHYSICAL",
                    db.movement_types.c.is_active.is_(True),
                )
            )
        if movement_id is None:
            raise ApiError(
                409, "purchase_movement_configuration", "Purchase receipt movement requires review"
            )
        entry = (
            await self.session.execute(
                insert(db.ledger)
                .values(
                    tenant_id=self.context.tenant_id,
                    occurred_at=occurred_at,
                    item_id=line["item_id"],
                    location_id=location_id,
                    stock_state="PHYSICAL",
                    direction="IN",
                    movement_type_id=movement_id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    source_document_type="PURCHASE_RECEIVE",
                    source_document_id=document["purchase_receive_id"],
                    source_document_line_id=line["purchase_receive_line_id"],
                    source_document_no=document["receive_no"],
                    idempotency_key=key,
                    created_by=self.context.user_id,
                )
                .returning(db.ledger.c.entry_id, db.ledger.c.occurred_at)
            )
        ).one()

        if method == "FIFO":
            await self.session.execute(
                insert(db.fifo_layers).values(
                    tenant_id=self.context.tenant_id,
                    item_id=line["item_id"],
                    location_id=location_id,
                    received_at=occurred_at,
                    original_qty=quantity,
                    remaining_qty=quantity,
                    unit_cost=unit_cost,
                    source_ledger_entry_id=entry.entry_id,
                    source_ledger_entry_at=entry.occurred_at,
                )
            )
        return entry.entry_id

    async def append_recovery_movement(
        self,
        *,
        code,
        name,
        direction,
        item_id,
        location_id,
        quantity,
        unit_cost,
        document_kind,
        document_id,
        line_id,
        document_no,
        occurred_at,
        key,
        reason_code_id=None,
        reason_note=None,
        reversal_of_entry_id=None,
    ):
        """Post a physical return or approved count adjustment through the ledger."""
        needs_reason = code in {"ADJUSTMENT_UP", "ADJUSTMENT_DOWN"}
        await self.session.execute(
            pg_insert(db.movement_types)
            .values(
                movement_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code=code,
                name=name,
                stock_state="PHYSICAL",
                direction_hint=direction,
                requires_reason=needs_reason,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        movement = (
            (
                await self.session.execute(
                    select(db.movement_types).where(
                        db.movement_types.c.tenant_id == self.context.tenant_id,
                        db.movement_types.c.code == code,
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            movement["stock_state"] != "PHYSICAL"
            or movement["direction_hint"] != direction
            or movement["requires_reason"] != needs_reason
            or not movement["is_active"]
        ):
            raise ApiError(
                409, "recovery_movement_configuration", "Movement configuration requires review"
            )
        if needs_reason and reason_code_id is None:
            raise ApiError(422, "adjustment_reason_required", "An adjustment reason is required")
        return (
            await self.session.execute(
                insert(db.ledger)
                .values(
                    tenant_id=self.context.tenant_id,
                    occurred_at=occurred_at,
                    item_id=item_id,
                    location_id=location_id,
                    stock_state="PHYSICAL",
                    direction=direction,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    movement_type_id=movement["movement_type_id"],
                    source_document_type=document_kind,
                    source_document_id=document_id,
                    source_document_line_id=line_id,
                    source_document_no=document_no,
                    idempotency_key=key,
                    reason_code_id=reason_code_id,
                    reason_note=reason_note,
                    reversal_of_entry_id=reversal_of_entry_id,
                    is_reversal=reversal_of_entry_id is not None,
                    created_by=self.context.user_id,
                )
                .returning(db.ledger.c.entry_id, db.ledger.c.occurred_at)
            )
        ).one()

    async def append_reservation(
        self,
        order,
        line_id,
        item_id,
        location_id,
        quantity,
        direction,
        occurred_at,
        key,
        shipment=None,
    ):
        """Post a commitment/release through the same ledger trigger as other stock states."""
        if direction not in {"IN", "OUT"} or quantity <= 0:
            raise ApiError(
                422, "invalid_reservation_movement", "Reservation quantity must be positive"
            )
        code = (
            "RESERVATION_HOLD"
            if direction == "IN"
            else "RESERVATION_SHIPPED"
            if shipment
            else "RESERVATION_RELEASE"
        )
        await self.session.execute(
            pg_insert(db.movement_types)
            .values(
                movement_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code=code,
                name=(
                    "Sales reservation"
                    if direction == "IN"
                    else "Sales shipment commitment"
                    if shipment
                    else "Sales release"
                ),
                stock_state="COMMITTED",
                direction_hint=direction,
                requires_reason=False,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        movement = (
            (
                await self.session.execute(
                    select(db.movement_types).where(
                        db.movement_types.c.tenant_id == self.context.tenant_id,
                        db.movement_types.c.code == code,
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            movement["stock_state"] != "COMMITTED"
            or movement["direction_hint"] != direction
            or movement["requires_reason"]
            or not movement["is_active"]
        ):
            raise ApiError(
                409,
                "reservation_movement_configuration",
                "Reservation movement configuration requires review",
            )
        return (
            await self.session.execute(
                insert(db.ledger)
                .values(
                    tenant_id=self.context.tenant_id,
                    occurred_at=occurred_at,
                    item_id=item_id,
                    location_id=location_id,
                    stock_state="COMMITTED",
                    direction=direction,
                    quantity=quantity,
                    movement_type_id=movement["movement_type_id"],
                    source_document_type="SHIPMENT" if shipment else "SALES_ORDER",
                    source_document_id=shipment["shipment_id"]
                    if shipment
                    else order["sales_order_id"],
                    source_document_line_id=line_id,
                    source_document_no=shipment["shipment_no"] if shipment else order["order_no"],
                    idempotency_key=key,
                    created_by=self.context.user_id,
                )
                .returning(db.ledger.c.entry_id)
            )
        ).scalar_one()

    async def append_shipment_issue(
        self,
        shipment,
        shipment_line_id,
        item_id,
        location_id,
        quantity,
        unit_cost,
        occurred_at,
        key,
    ):
        """Post physical stock only; invoicing owns the accounting issue later."""
        await self.session.execute(
            pg_insert(db.movement_types)
            .values(
                movement_type_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                code="SALE_SHIPMENT",
                name="Sales shipment",
                stock_state="PHYSICAL",
                direction_hint="OUT",
                requires_reason=False,
                is_system=True,
                is_active=True,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "code"])
        )
        movement = (
            (
                await self.session.execute(
                    select(db.movement_types).where(
                        db.movement_types.c.tenant_id == self.context.tenant_id,
                        db.movement_types.c.code == "SALE_SHIPMENT",
                    )
                )
            )
            .mappings()
            .one()
        )
        if (
            movement["stock_state"] != "PHYSICAL"
            or movement["direction_hint"] != "OUT"
            or movement["requires_reason"]
            or not movement["is_active"]
        ):
            raise ApiError(
                409,
                "shipment_movement_configuration",
                "Sales shipment movement configuration requires review",
            )
        return (
            await self.session.execute(
                insert(db.ledger)
                .values(
                    tenant_id=self.context.tenant_id,
                    occurred_at=occurred_at,
                    item_id=item_id,
                    location_id=location_id,
                    stock_state="PHYSICAL",
                    direction="OUT",
                    quantity=quantity,
                    unit_cost=unit_cost,
                    movement_type_id=movement["movement_type_id"],
                    source_document_type="SHIPMENT",
                    source_document_id=shipment["shipment_id"],
                    source_document_line_id=shipment_line_id,
                    source_document_no=shipment["shipment_no"],
                    idempotency_key=key,
                    created_by=self.context.user_id,
                )
                .returning(db.ledger.c.entry_id, db.ledger.c.occurred_at)
            )
        ).one()


class OpeningStockService:
    def __init__(self, session: AsyncSession, context: CatalogContext):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)
        self.posting = InventoryPostingService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    def authorize(self):
        if not self.context.can_write:
            raise ApiError(
                403, "opening_stock_forbidden", "Only an Owner or Admin can post opening stock"
            )

    async def locations(self):
        rows = await self.session.execute(
            select(db.locations).where(self.scope(db.locations)).order_by(db.locations.c.code)
        )
        return [dict(row) for row in rows.mappings()]

    async def create_location(self, payload: LocationIn):
        self.authorize()
        await self.catalog.lock()
        existing = (
            (
                await self.session.execute(
                    select(db.location_types).where(
                        self.scope(db.location_types),
                        func.upper(db.location_types.c.code) == payload.type,
                    )
                )
            )
            .mappings()
            .all()
        )
        if len(existing) > 1:
            raise ApiError(409, "ambiguous_location_type", "Location-type codes require review")
        if existing:
            kind = existing[0]
            if kind["is_in_transit"]:
                raise ApiError(
                    409, "invalid_location_type", "A basic location cannot use an in-transit type"
                )
            type_id = kind["location_type_id"]
        else:
            type_id = uuid.uuid4()
            await self.session.execute(
                insert(db.location_types).values(
                    location_type_id=type_id,
                    tenant_id=self.context.tenant_id,
                    code=payload.type,
                    name=payload.type.title(),
                    is_in_transit=False,
                    is_storage=payload.type != "OFFICE",
                    is_system=True,
                )
            )
        row = (
            (
                await self.session.execute(
                    insert(db.locations)
                    .values(
                        location_id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        location_type_id=type_id,
                        code=payload.code,
                        name=payload.name,
                        is_active=True,
                        address_line1=payload.address,
                        city=payload.city,
                        state_code=payload.state_code,
                        pincode=payload.pincode,
                    )
                    .returning(db.locations)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)

    async def document(self, document_id, *, lock=False):
        document = await self.catalog.row(
            db.opening_stock, "opening_stock_id", document_id, lock=lock
        )
        rows = await self.session.execute(
            select(db.opening_lines)
            .where(
                self.scope(db.opening_lines),
                db.opening_lines.c.opening_stock_id == document_id,
            )
            .order_by(db.opening_lines.c.line_no)
        )
        document["lines"] = [dict(row) for row in rows.mappings()]
        return document

    async def list_documents(self, page, page_size):
        count = await self.session.scalar(
            select(func.count()).select_from(db.opening_stock).where(self.scope(db.opening_stock))
        )
        ids = (
            await self.session.execute(
                select(db.opening_stock.c.opening_stock_id)
                .where(self.scope(db.opening_stock))
                .order_by(db.opening_stock.c.created_at.desc(), db.opening_stock.c.opening_stock_id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars()
        return {
            "items": [await self.document(value) for value in ids],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def snapshots(self, payload: OpeningIn):
        today = datetime.now(UTC).astimezone(timezone(timedelta(hours=5, minutes=30))).date()
        if payload.business_date > today:
            raise ApiError(
                422, "future_opening_date", "The opening-stock date cannot be in the future"
            )
        snapshots = []
        for number, line in enumerate(payload.lines, start=1):
            item = await self.catalog.reference(catalog_db.items, "item_id", line.item_id)
            kind = await self.catalog.reference(
                catalog_db.item_types, "item_type_id", item["item_type_id"]
            )
            if (
                item["status"] != "ACTIVE"
                or item["item_kind"] == "COMPOSITE"
                or not kind["is_stockable"]
            ):
                raise ApiError(
                    422,
                    "opening_item_not_stockable",
                    "Opening stock requires an active, stockable component item",
                )
            if any(
                item[field]
                for field in ["track_batch", "track_lot", "track_serial", "track_expiry"]
            ):
                raise ApiError(
                    422,
                    "tracking_workflow_required",
                    "Tracked items require their tracking workflow before stock can be posted",
                )
            location = await self.catalog.reference(db.locations, "location_id", line.location_id)
            location_type = await self.catalog.row(
                db.location_types, "location_type_id", location["location_type_id"]
            )
            if not location_type["is_storage"] or location_type["is_in_transit"]:
                raise ApiError(
                    422,
                    "opening_location_not_storage",
                    "Choose an active storage location outside transit",
                )
            source = await self.catalog.reference(catalog_db.units, "uom_id", line.uom_id)
            target = await self.catalog.reference(catalog_db.units, "uom_id", item["stock_uom_id"])
            factor = await self.catalog.factor_for(item, line.uom_id)
            exact_stock_quantity(line.quantity, Decimal(1), source["decimal_places"])
            converted = exact_stock_quantity(line.quantity, factor, target["decimal_places"])
            with localcontext() as context:
                context.prec = 80
                if converted * line.unit_cost >= Decimal("10000000000000000"):
                    raise ApiError(
                        422,
                        "opening_value_out_of_range",
                        "Quantity multiplied by cost exceeds the supported posting value",
                    )
            snapshots.append(
                {
                    "opening_stock_line_id": uuid.uuid4(),
                    "tenant_id": self.context.tenant_id,
                    "line_no": number,
                    "item_id": line.item_id,
                    "location_id": line.location_id,
                    "entered_uom_id": line.uom_id,
                    "entered_quantity": line.quantity,
                    "conversion_factor": factor,
                    "stock_uom_id": item["stock_uom_id"],
                    "stock_quantity": converted,
                    "unit_cost": line.unit_cost,
                    "item_catalog_version": item["catalog_version"],
                    "sku_snapshot": item["sku"],
                    "item_name_snapshot": item["name"],
                    "stock_unit_snapshot": target["code"],
                    "location_code_snapshot": location["code"],
                }
            )
        return snapshots

    async def reserve_number(self, business_date):
        tenant = (
            (
                await self.session.execute(
                    select(catalog_db.tenants).where(self.scope(catalog_db.tenants))
                )
            )
            .mappings()
            .one()
        )
        start = business_date.year - (business_date.month < tenant["fiscal_year_start_month"])
        fiscal_year = f"{start}-{(start + 1) % 100:02d}"
        statement = (
            pg_insert(db.number_sequences)
            .values(
                tenant_id=self.context.tenant_id,
                document_kind="OPENING_STOCK",
                fiscal_year=fiscal_year,
                prefix="OPEN",
                next_value=2,
                padding=5,
                suffix="",
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "document_kind", "fiscal_year"],
                set_={"next_value": db.number_sequences.c.next_value + 1},
            )
        )
        row = (
            (await self.session.execute(statement.returning(db.number_sequences))).mappings().one()
        )
        sequence = str(row["next_value"] - 1).zfill(row["padding"])
        return f"{row['prefix']}/{fiscal_year}/{sequence}{row['suffix']}"

    async def save(self, payload: OpeningIn, document_id=None):
        self.authorize()
        await self.catalog.lock()
        if document_id:
            old = await self.document(document_id, lock=True)
            if old["status"] != "DRAFT":
                raise ApiError(
                    409, "opening_not_draft", "Only draft opening-stock documents can be edited"
                )
            if not isinstance(payload, OpeningUpdate) or old["version"] != payload.expected_version:
                raise ApiError(
                    409, "stale_opening_stock", "This document changed. Reload it before saving"
                )
        snapshots = await self.snapshots(payload)
        if document_id:
            await self.session.execute(
                delete(db.opening_lines).where(
                    self.scope(db.opening_lines), db.opening_lines.c.opening_stock_id == document_id
                )
            )
            await self.session.execute(
                update(db.opening_stock)
                .where(
                    self.scope(db.opening_stock), db.opening_stock.c.opening_stock_id == document_id
                )
                .values(
                    business_date=payload.business_date,
                    notes=payload.notes,
                    version=old["version"] + 1,
                )
            )
        else:
            document_id = uuid.uuid4()
            await self.session.execute(
                insert(db.opening_stock).values(
                    opening_stock_id=document_id,
                    tenant_id=self.context.tenant_id,
                    document_no=await self.reserve_number(payload.business_date),
                    business_date=payload.business_date,
                    notes=payload.notes,
                    status="DRAFT",
                    version=1,
                    created_by=self.context.user_id,
                )
            )
        for snapshot in snapshots:
            await self.session.execute(
                insert(db.opening_lines).values(opening_stock_id=document_id, **snapshot)
            )
        return await self.document(document_id)

    async def cancel(self, document_id, expected_version):
        self.authorize()
        document = await self.document(document_id, lock=True)
        if document["status"] != "DRAFT" or document["version"] != expected_version:
            raise ApiError(
                409, "opening_not_current_draft", "Reload the current draft before cancelling it"
            )
        await self.session.execute(
            update(db.opening_stock)
            .where(self.scope(db.opening_stock), db.opening_stock.c.opening_stock_id == document_id)
            .values(status="CANCELLED", version=expected_version + 1)
        )
        return await self.document(document_id)

    async def validate_pair_history(self, line):
        pair = {
            "tenant": str(self.context.tenant_id),
            "item": str(line["item_id"]),
            "location": str(line["location_id"]),
        }
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cims.stock:{pair['tenant']}:{pair['item']}:{pair['location']}"},
        )
        where = [
            self.scope(db.ledger),
            db.ledger.c.item_id == line["item_id"],
            db.ledger.c.location_id == line["location_id"],
        ]
        operational = await self.session.scalar(
            select(
                exists(
                    select(1)
                    .select_from(db.ledger)
                    .where(
                        *where,
                        db.ledger.c.source_document_type.is_distinct_from("OPENING_STOCK"),
                    )
                )
            )
        )
        if operational:
            raise ApiError(
                409,
                "opening_after_operations",
                "Opening stock is unavailable after operational movements have begun",
            )
        expected = dict(
            (
                await self.session.execute(
                    select(db.ledger.c.stock_state, func.sum(db.ledger.c.signed_quantity))
                    .where(*where)
                    .group_by(db.ledger.c.stock_state)
                )
            ).all()
        )
        balance = (
            (
                await self.session.execute(
                    select(db.balances)
                    .where(
                        self.scope(db.balances),
                        db.balances.c.item_id == line["item_id"],
                        db.balances.c.location_id == line["location_id"],
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        for state, column in [
            ("PHYSICAL", "physical_on_hand"),
            ("ACCOUNTING", "accounting_on_hand"),
            ("COMMITTED", "committed"),
            ("INCOMING", "incoming"),
        ]:
            if (balance[column] if balance else 0) != expected.get(state, 0):
                raise ApiError(
                    409,
                    "stock_history_mismatch",
                    "Recorded stock differs from the ledger. Reconcile it before posting",
                )

    async def post(self, document_id, expected_version, key):
        self.authorize()
        # Use the same lock order as catalog/draft edits so references cannot change
        # between validation and posting through another application request.
        await self.catalog.lock()
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "action": "opening.post",
                    "document": str(document_id),
                    "version": expected_version,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cims.idempotency:{self.context.tenant_id}:{key}"},
        )
        receipt = (
            (
                await self.session.execute(
                    select(db.idempotency_keys)
                    .where(self.scope(db.idempotency_keys), db.idempotency_keys.c.key == key)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if receipt:
            if receipt["request_fingerprint"] != fingerprint or receipt["scope"] != "POSTING":
                raise ApiError(
                    409,
                    "idempotency_conflict",
                    "This operation key was already used with different input",
                )
            if receipt["response_body"] is not None:
                return receipt["response_body"]
            raise ApiError(
                409, "operation_in_progress", "This operation has not completed; try again"
            )
        await self.session.execute(
            insert(db.idempotency_keys).values(
                tenant_id=self.context.tenant_id,
                key=key,
                scope="POSTING",
                request_fingerprint=fingerprint,
            )
        )
        document = await self.document(document_id, lock=True)
        if document["status"] != "DRAFT" or document["version"] != expected_version:
            raise ApiError(
                409, "opening_not_current_draft", "Reload the current draft before posting it"
            )
        if not document["lines"]:
            raise ApiError(422, "opening_lines_required", "Add at least one opening-stock line")
        reviewed = await self.snapshots(
            OpeningIn(
                business_date=document["business_date"],
                lines=[
                    {
                        "item_id": line["item_id"],
                        "location_id": line["location_id"],
                        "uom_id": line["entered_uom_id"],
                        "quantity": line["entered_quantity"],
                        "unit_cost": line["unit_cost"],
                    }
                    for line in document["lines"]
                ],
            )
        )
        for saved, current in zip(document["lines"], reviewed, strict=True):
            if any(
                saved[field] != current[field]
                for field in (
                    "item_catalog_version",
                    "stock_uom_id",
                    "conversion_factor",
                    "stock_quantity",
                )
            ):
                raise ApiError(
                    409,
                    "opening_catalog_changed",
                    "An item changed. Review and save the draft again",
                )
        item_rows = {}
        for item_id in sorted({line["item_id"] for line in document["lines"]}):
            item_rows[item_id] = await self.catalog.row(
                catalog_db.items, "item_id", item_id, lock=True
            )
        for location_id in sorted({line["location_id"] for line in document["lines"]}):
            location = await self.catalog.row(db.locations, "location_id", location_id, lock=True)
            if not location["is_active"]:
                raise ApiError(
                    409,
                    "inactive_opening_location",
                    "An opening-stock location has been deactivated",
                )
        ordered = sorted(document["lines"], key=lambda line: (line["item_id"], line["location_id"]))
        for line in ordered:
            item = item_rows[line["item_id"]]
            if (
                item["catalog_version"] != line["item_catalog_version"]
                or item["status"] != "ACTIVE"
            ):
                raise ApiError(
                    409,
                    "opening_catalog_changed",
                    "An item changed after the draft was prepared. Review and save the draft again",
                )
            await self.validate_pair_history(line)
        occurred_at = await self.session.scalar(select(func.clock_timestamp()))
        methods = {}
        for item_id, item in item_rows.items():
            group_method = None
            if item["item_group_id"]:
                group_method = await self.session.scalar(
                    select(catalog_db.groups.c.valuation_method).where(
                        self.scope(catalog_db.groups),
                        catalog_db.groups.c.item_group_id == item["item_group_id"],
                    )
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
            methods[item_id] = method
            if item["valuation_locked_at"] is None:
                await self.session.execute(
                    update(catalog_db.items)
                    .where(self.scope(catalog_db.items), catalog_db.items.c.item_id == item_id)
                    .values(
                        valuation_method=method,
                        valuation_locked_at=occurred_at,
                        catalog_version=item["catalog_version"] + 1,
                    )
                )
        for line in ordered:
            if methods[line["item_id"]] == "FIFO":
                layers = await self.session.scalar(
                    select(
                        func.coalesce(
                            func.sum(db.fifo_layers.c.remaining_qty),
                            0,
                        )
                    ).where(
                        self.scope(db.fifo_layers),
                        db.fifo_layers.c.item_id == line["item_id"],
                        db.fifo_layers.c.location_id == line["location_id"],
                    )
                )
                physical = await self.session.scalar(
                    select(db.balances.c.physical_on_hand).where(
                        self.scope(db.balances),
                        db.balances.c.item_id == line["item_id"],
                        db.balances.c.location_id == line["location_id"],
                    )
                )
                if layers != (physical or 0):
                    raise ApiError(
                        409,
                        "fifo_history_mismatch",
                        "FIFO layers differ from physical stock. Reconcile them first",
                    )
            await self.posting.append_opening(
                document, line, methods[line["item_id"]], occurred_at, key
            )
        await self.session.execute(
            update(db.opening_stock)
            .where(self.scope(db.opening_stock), db.opening_stock.c.opening_stock_id == document_id)
            .values(
                status="POSTED",
                posted_at=occurred_at,
                posted_by=self.context.user_id,
                version=expected_version + 1,
            )
        )
        await self.session.execute(
            insert(db.outbox_events).values(
                outbox_event_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                event_type="inventory.opening_posted",
                aggregate_id=document_id,
                payload={
                    "opening_stock_id": str(document_id),
                    "item_locations": [
                        {"item_id": str(line["item_id"]), "location_id": str(line["location_id"])}
                        for line in ordered
                    ],
                },
            )
        )
        result = await self.document(document_id)
        body = OpeningOut.model_validate(result).model_dump(mode="json")
        await self.session.execute(
            update(db.idempotency_keys)
            .where(self.scope(db.idempotency_keys), db.idempotency_keys.c.key == key)
            .values(response_status=200, response_body=body)
        )
        return body
