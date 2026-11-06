"""Chunk 3A read side. No endpoint here accepts arbitrary ledger entries."""

from app.api.catalog_access import CatalogContext
from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as db
from app.services.catalog import CatalogService
from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession


class InventoryService:
    def __init__(self, session: AsyncSession, context: CatalogContext):
        self.session = session
        self.context = context

    async def prepare(self):
        # Location-scoped authenticated access waits for the deferred identity integration.
        # Local mode is explicitly computer-only; normal access currently fails closed for
        # roles whose location permissions cannot yet be resolved against the existing DB.
        if not self.context.local and self.context.role not in {"Owner", "Admin"}:
            raise ApiError(
                403, "inventory_access_pending", "Inventory access is not configured for this role"
            )
        await CatalogService(self.session, self.context).prepare()

    async def page(self, statement, page: int, page_size: int):
        count = await self.session.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        rows = (
            await self.session.execute(statement.offset((page - 1) * page_size).limit(page_size))
        ).mappings()
        return {
            "items": [dict(row) for row in rows],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def balances(self, page: int, page_size: int, item_id=None, location_id=None):
        tenant = self.context.tenant_id
        layers = (
            select(
                db.fifo_layers.c.item_id,
                db.fifo_layers.c.location_id,
                func.sum(db.fifo_layers.c.remaining_qty).label("quantity"),
                func.sum(db.fifo_layers.c.remaining_qty * db.fifo_layers.c.unit_cost).label(
                    "value"
                ),
            )
            .where(db.fifo_layers.c.tenant_id == tenant)
            .group_by(
                db.fifo_layers.c.item_id,
                db.fifo_layers.c.location_id,
            )
            .cte("remaining_layers")
        )
        method = func.coalesce(
            catalog_db.items.c.valuation_method,
            catalog_db.groups.c.valuation_method,
            catalog_db.tenants.c.default_valuation_method,
        )
        complete = or_(
            method != "FIFO", func.coalesce(layers.c.quantity, 0) == db.balances.c.physical_on_hand
        )
        # Unknown custom location types remain ineligible until explicitly supported.
        eligible = and_(
            db.locations.c.is_active,
            db.location_types.c.is_storage,
            ~db.location_types.c.is_in_transit,
            func.upper(db.location_types.c.code).in_(["WAREHOUSE", "GODOWN", "STORE"]),
        )
        available = db.balances.c.physical_on_hand - db.balances.c.committed
        valuation = case(
            (
                method == "FIFO",
                case((complete, func.round(func.coalesce(layers.c.value, 0), 4)), else_=None),
            ),
            else_=db.balances.c.inventory_value,
        )
        statement = select(
            db.balances.c.item_id,
            catalog_db.items.c.sku,
            catalog_db.items.c.name.label("item_name"),
            db.balances.c.location_id,
            db.locations.c.code.label("location_code"),
            db.locations.c.name.label("location_name"),
            db.balances.c.physical_on_hand,
            db.balances.c.accounting_on_hand,
            db.balances.c.committed,
            db.balances.c.incoming,
            available.label("available"),
            catalog_db.items.c.safety_stock,
            case((eligible, available - catalog_db.items.c.safety_stock), else_=0).label("atp"),
            eligible.label("eligible_for_sales"),
            method.label("valuation_method"),
            valuation.label("inventory_value"),
            complete.label("valuation_complete"),
            db.balances.c.last_movement_at,
        ).select_from(
            db.balances.join(
                catalog_db.items,
                and_(
                    catalog_db.items.c.tenant_id == tenant,
                    catalog_db.items.c.item_id == db.balances.c.item_id,
                ),
            )
            .join(
                db.locations,
                and_(
                    db.locations.c.tenant_id == tenant,
                    db.locations.c.location_id == db.balances.c.location_id,
                ),
            )
            .join(
                db.location_types,
                and_(
                    db.location_types.c.tenant_id == tenant,
                    db.location_types.c.location_type_id == db.locations.c.location_type_id,
                ),
            )
            .join(catalog_db.tenants, catalog_db.tenants.c.tenant_id == tenant)
            .outerjoin(
                catalog_db.groups,
                and_(
                    catalog_db.groups.c.tenant_id == tenant,
                    catalog_db.groups.c.item_group_id == catalog_db.items.c.item_group_id,
                ),
            )
            .outerjoin(
                layers,
                and_(
                    layers.c.item_id == db.balances.c.item_id,
                    layers.c.location_id == db.balances.c.location_id,
                ),
            )
        )
        statement = statement.where(db.balances.c.tenant_id == tenant)
        if item_id:
            statement = statement.where(db.balances.c.item_id == item_id)
        if location_id:
            statement = statement.where(db.balances.c.location_id == location_id)
        return await self.page(
            statement.order_by(
                catalog_db.items.c.sku,
                db.locations.c.code,
                db.balances.c.item_id,
                db.balances.c.location_id,
            ),
            page,
            page_size,
        )

    async def movements(self, page, page_size, item_id=None, location_id=None, stock_state=None):
        tenant = self.context.tenant_id
        statement = select(
            db.ledger.c.entry_id,
            db.ledger.c.occurred_at,
            db.ledger.c.item_id,
            catalog_db.items.c.sku,
            db.ledger.c.location_id,
            db.locations.c.code.label("location_code"),
            db.ledger.c.stock_state,
            db.ledger.c.direction,
            db.ledger.c.quantity,
            db.ledger.c.unit_cost,
            db.ledger.c.source_document_type,
            db.ledger.c.source_document_id,
            db.ledger.c.source_document_no,
            db.ledger.c.physical_balance_after,
        ).select_from(
            db.ledger.join(
                catalog_db.items,
                and_(
                    catalog_db.items.c.tenant_id == tenant,
                    catalog_db.items.c.item_id == db.ledger.c.item_id,
                ),
            ).join(
                db.locations,
                and_(
                    db.locations.c.tenant_id == tenant,
                    db.locations.c.location_id == db.ledger.c.location_id,
                ),
            )
        )
        statement = statement.where(db.ledger.c.tenant_id == tenant)
        if item_id:
            statement = statement.where(db.ledger.c.item_id == item_id)
        if location_id:
            statement = statement.where(db.ledger.c.location_id == location_id)
        if stock_state:
            statement = statement.where(db.ledger.c.stock_state == stock_state)
        return await self.page(
            statement.order_by(db.ledger.c.occurred_at.desc(), db.ledger.c.entry_id.desc()),
            page,
            page_size,
        )

    async def reconcile(self, page, page_size, item_locations=None):
        tenant = self.context.tenant_id
        names = {
            "PHYSICAL": "physical",
            "ACCOUNTING": "accounting",
            "COMMITTED": "committed",
            "INCOMING": "incoming",
        }
        expected = (
            select(
                db.ledger.c.item_id,
                db.ledger.c.location_id,
                *(
                    func.sum(
                        case(
                            (db.ledger.c.stock_state == state, db.ledger.c.signed_quantity), else_=0
                        )
                    ).label(name)
                    for state, name in names.items()
                ),
            )
            .where(db.ledger.c.tenant_id == tenant)
            .group_by(db.ledger.c.item_id, db.ledger.c.location_id)
        )
        if item_locations:
            expected = expected.where(
                tuple_(db.ledger.c.item_id, db.ledger.c.location_id).in_(item_locations)
            )
        expected = expected.cte("ledger_totals")
        actual = select(db.balances).where(db.balances.c.tenant_id == tenant)
        if item_locations:
            actual = actual.where(
                tuple_(db.balances.c.item_id, db.balances.c.location_id).in_(item_locations)
            )
        actual = actual.cte("observed_balances")
        differences = {
            "physical_difference": func.coalesce(actual.c.physical_on_hand, 0)
            - func.coalesce(expected.c.physical, 0),
            "accounting_difference": func.coalesce(actual.c.accounting_on_hand, 0)
            - func.coalesce(expected.c.accounting, 0),
            "committed_difference": func.coalesce(actual.c.committed, 0)
            - func.coalesce(expected.c.committed, 0),
            "incoming_difference": func.coalesce(actual.c.incoming, 0)
            - func.coalesce(expected.c.incoming, 0),
        }
        statement = select(
            func.coalesce(actual.c.item_id, expected.c.item_id).label("item_id"),
            func.coalesce(actual.c.location_id, expected.c.location_id).label("location_id"),
            *(value.label(key) for key, value in differences.items()),
        ).select_from(
            actual.join(
                expected,
                and_(
                    actual.c.item_id == expected.c.item_id,
                    actual.c.location_id == expected.c.location_id,
                ),
                full=True,
            )
        )
        statement = statement.where(or_(*(value != 0 for value in differences.values())))
        return await self.page(statement.order_by("item_id", "location_id"), page, page_size)
