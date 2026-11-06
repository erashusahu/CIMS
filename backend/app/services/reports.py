"""Tenant-scoped operational counts; financial settlement remains external."""

from sqlalchemy import Integer, and_, cast, func, select

from app.models import catalog, inventory, procurement, recovery, sales
from app.services.inventory import InventoryService


class ReportsService:
    def __init__(self, session, context):
        self.session, self.context = session, context

    async def prepare(self):
        # Until location grants are mapped, existing InventoryService fails closed for
        # authenticated non-admin roles. Whole-business totals need that same gate.
        await InventoryService(self.session, self.context).prepare()

    async def overview(self):
        tenant = self.context.tenant_id
        statement = select(
            func.clock_timestamp().label("as_of"),
            select(func.count())
            .select_from(catalog.items)
            .where(catalog.items.c.tenant_id == tenant, catalog.items.c.status == "ACTIVE")
            .scalar_subquery()
            .label("active_items"),
            select(func.count())
            .select_from(inventory.balances)
            .where(inventory.balances.c.tenant_id == tenant)
            .scalar_subquery()
            .label("stock_positions"),
            select(func.count())
            .select_from(
                inventory.balances.join(
                    catalog.items,
                    (catalog.items.c.tenant_id == inventory.balances.c.tenant_id)
                    & (catalog.items.c.item_id == inventory.balances.c.item_id),
                )
                .join(
                    inventory.locations,
                    and_(
                        inventory.locations.c.tenant_id == tenant,
                        inventory.locations.c.location_id == inventory.balances.c.location_id,
                    ),
                )
                .join(
                    inventory.location_types,
                    and_(
                        inventory.location_types.c.tenant_id == tenant,
                        inventory.location_types.c.location_type_id
                        == inventory.locations.c.location_type_id,
                    ),
                )
            )
            .where(
                inventory.balances.c.tenant_id == tenant,
                catalog.items.c.status == "ACTIVE",
                inventory.locations.c.is_active,
                inventory.location_types.c.is_storage,
                ~inventory.location_types.c.is_in_transit,
                func.upper(inventory.location_types.c.code).in_(["WAREHOUSE", "GODOWN", "STORE"]),
                inventory.balances.c.physical_on_hand - inventory.balances.c.committed
                <= catalog.items.c.safety_stock,
            )
            .scalar_subquery()
            .label("below_safety_positions"),
            select(func.count())
            .select_from(procurement.purchase_orders)
            .where(
                procurement.purchase_orders.c.tenant_id == tenant,
                procurement.purchase_orders.c.status.in_(["SENT", "PARTIALLY_RECEIVED"]),
            )
            .scalar_subquery()
            .label("open_purchase_orders"),
            select(func.count())
            .select_from(sales.orders)
            .where(
                sales.orders.c.tenant_id == tenant,
                sales.orders.c.status.in_(["CONFIRMED", "PARTIALLY_FULFILLED"]),
            )
            .scalar_subquery()
            .label("confirmed_sales_orders"),
            select(func.count())
            .select_from(recovery.counts)
            .where(
                recovery.counts.c.tenant_id == tenant,
                recovery.counts.c.status.in_(["IN_PROGRESS", "SUBMITTED", "APPROVED"]),
            )
            .scalar_subquery()
            .label("active_counts"),
            select(func.count())
            .select_from(inventory.outbox_events)
            .where(
                inventory.outbox_events.c.tenant_id == tenant,
                inventory.outbox_events.c.status.in_(["PENDING", "RETRY"]),
            )
            .scalar_subquery()
            .label("pending_outbox_events"),
        )
        row = (await self.session.execute(statement)).mappings().one()
        return {
            "scope": "WHOLE_BUSINESS",
            "as_of": row["as_of"],
            "active_items": row["active_items"],
            "stock_positions": row["stock_positions"],
            "below_safety_positions": row["below_safety_positions"],
            "open_purchase_orders": row["open_purchase_orders"],
            "confirmed_sales_orders": row["confirmed_sales_orders"],
            "active_counts": row["active_counts"],
            "pending_outbox_events": row["pending_outbox_events"],
        }

    async def aging(self, page, page_size, item_id=None, location_id=None):
        """Remaining FIFO layers only; weighted-average stock has no receipt-layer age."""
        tenant = self.context.tenant_id
        age_days = cast(
            func.greatest(
                0,
                func.floor(
                    func.extract(
                        "epoch", func.clock_timestamp() - inventory.fifo_layers.c.received_at
                    )
                    / 86400
                ),
            ),
            Integer,
        )
        statement = (
            select(
                inventory.fifo_layers.c.fifo_layer_id,
                inventory.fifo_layers.c.item_id,
                catalog.items.c.sku,
                inventory.fifo_layers.c.location_id,
                inventory.locations.c.code.label("location_code"),
                inventory.fifo_layers.c.received_at,
                age_days.label("age_days"),
                inventory.fifo_layers.c.remaining_qty.label("remaining_quantity"),
                inventory.fifo_layers.c.unit_cost,
                (inventory.fifo_layers.c.remaining_qty * inventory.fifo_layers.c.unit_cost).label(
                    "remaining_value"
                ),
            )
            .select_from(
                inventory.fifo_layers.join(
                    catalog.items,
                    and_(
                        catalog.items.c.tenant_id == tenant,
                        catalog.items.c.item_id == inventory.fifo_layers.c.item_id,
                    ),
                ).join(
                    inventory.locations,
                    and_(
                        inventory.locations.c.tenant_id == tenant,
                        inventory.locations.c.location_id == inventory.fifo_layers.c.location_id,
                    ),
                )
            )
            .where(
                inventory.fifo_layers.c.tenant_id == tenant,
                inventory.fifo_layers.c.remaining_qty > 0,
            )
        )
        if item_id:
            statement = statement.where(inventory.fifo_layers.c.item_id == item_id)
        if location_id:
            statement = statement.where(inventory.fifo_layers.c.location_id == location_id)
        return await InventoryService(self.session, self.context).page(
            statement.order_by(
                inventory.fifo_layers.c.received_at,
                inventory.fifo_layers.c.fifo_layer_id,
            ),
            page,
            page_size,
        )

    async def sales_summary(self, from_date=None, to_date=None):
        tenant = self.context.tenant_id
        statement = (
            select(
                sales.orders.c.status,
                sales.orders.c.sales_source.label("source"),
                func.count().label("order_count"),
                func.coalesce(func.sum(sales.orders.c.grand_total), 0).label("order_value"),
                func.coalesce(func.sum(sales.orders.c.tax_total), 0).label("estimated_tax"),
            )
            .where(sales.orders.c.tenant_id == tenant)
            .group_by(sales.orders.c.status, sales.orders.c.sales_source)
            .order_by(sales.orders.c.status, sales.orders.c.sales_source)
        )
        if from_date:
            statement = statement.where(sales.orders.c.order_date >= from_date)
        if to_date:
            statement = statement.where(sales.orders.c.order_date <= to_date)
        return [dict(row) for row in (await self.session.execute(statement)).mappings()]

    async def purchase_summary(self, from_date=None, to_date=None):
        tenant = self.context.tenant_id
        currency = func.coalesce(procurement.purchase_orders.c.currency, "UNSPECIFIED")
        statement = (
            select(
                procurement.purchase_orders.c.status,
                currency.label("currency"),
                func.count().label("order_count"),
                func.coalesce(func.sum(procurement.purchase_orders.c.grand_total), 0).label(
                    "order_value"
                ),
                func.coalesce(func.sum(procurement.purchase_orders.c.tax_total), 0).label(
                    "estimated_tax"
                ),
            )
            .where(procurement.purchase_orders.c.tenant_id == tenant)
            .group_by(procurement.purchase_orders.c.status, currency)
            .order_by(procurement.purchase_orders.c.status, currency)
        )
        if from_date:
            statement = statement.where(procurement.purchase_orders.c.order_date >= from_date)
        if to_date:
            statement = statement.where(procurement.purchase_orders.c.order_date <= to_date)
        return [dict(row) for row in (await self.session.execute(statement)).mappings()]

    async def order_tax_support(self, from_date=None, to_date=None):
        """Confirmed sales-order snapshots; these are not invoice or filed tax totals."""
        tenant = self.context.tenant_id
        statement = (
            select(
                sales.lines.c.tax_rate,
                func.count().label("line_count"),
                func.coalesce(func.sum(sales.lines.c.taxable_value), 0).label("taxable_value"),
                func.coalesce(func.sum(sales.lines.c.tax_amount), 0).label("estimated_tax"),
            )
            .select_from(
                sales.lines.join(
                    sales.orders,
                    and_(
                        sales.orders.c.tenant_id == tenant,
                        sales.orders.c.sales_order_id == sales.lines.c.sales_order_id,
                    ),
                )
            )
            .where(
                sales.lines.c.tenant_id == tenant,
                sales.orders.c.status.in_(["CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED"]),
            )
            .group_by(sales.lines.c.tax_rate)
            .order_by(sales.lines.c.tax_rate)
        )
        if from_date:
            statement = statement.where(sales.orders.c.order_date >= from_date)
        if to_date:
            statement = statement.where(sales.orders.c.order_date <= to_date)
        return [dict(row) for row in (await self.session.execute(statement)).mappings()]
