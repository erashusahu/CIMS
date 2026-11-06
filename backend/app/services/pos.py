"""POS draft setup. Final checkout belongs to the later billing integration."""

import uuid

from sqlalchemy import func, select, update

from app.core.errors import ApiError
from app.models import customer as customer_db
from app.models import sales as sales_db
from app.schemas.customer import CustomerIn
from app.services.customer import CustomerService
from app.services.sales_orders import SalesOrderService


class PosService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.sales = SalesOrderService(session, context)
        self.customers = CustomerService(session, context)

    async def walk_in_customer(self, location_id: uuid.UUID):
        self.sales.authorize_write()
        await self.sales.catalog.lock()
        location = await self.sales.location(location_id)
        existing = (
            (
                await self.session.execute(
                    select(customer_db.customers)
                    .where(
                        customer_db.customers.c.tenant_id == self.context.tenant_id,
                        func.lower(customer_db.customers.c.code) == "pos-walkin",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["party_type"] != "B2C" or not existing["is_active"]:
                raise ApiError(
                    409,
                    "walk_in_customer_conflict",
                    "POS-WALKIN is already used by another customer",
                )
            return dict(existing)
        return await self.customers.save(
            CustomerIn(
                code="POS-WALKIN",
                name="Walk-in customer",
                party_type="B2C",
                tax_treatment="UNREGISTERED",
                place_of_supply_state=location["state_code"],
                payment_terms_days=0,
                credit_limit=0,
            )
        )

    async def discard(self, order_id: uuid.UUID, expected_revision: int):
        self.sales.authorize_write()
        await self.sales.catalog.lock()
        order = await self.sales.order(order_id, lock=True)
        if order["sales_source"] != "POS":
            raise ApiError(422, "not_pos_cart", "Choose a POS cart")
        if order["status"] == "CANCELLED":
            return order
        if order["status"] != "DRAFT" or order["revision"] != expected_revision:
            raise ApiError(409, "pos_cart_stale", "Reload the current POS cart")
        await self.session.execute(
            update(sales_db.orders)
            .where(
                sales_db.orders.c.tenant_id == self.context.tenant_id,
                sales_db.orders.c.sales_order_id == order_id,
            )
            .values(
                status="CANCELLED",
                revision=expected_revision + 1,
                cancelled_at=func.clock_timestamp(),
                cancel_reason="POS draft discarded",
            )
        )
        return await self.sales.order(order_id)
