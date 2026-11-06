"""Reorder rules and live, supplier-grouped purchase suggestions."""

import uuid
from decimal import Decimal

from sqlalchemy import func, insert, select, update

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as inventory_db
from app.models import procurement as db
from app.services.procurement import ProcurementService


class ReorderService(ProcurementService):
    async def rules(self, page, page_size):
        item, location, vendor, rule = (
            catalog_db.items,
            inventory_db.locations,
            db.vendors,
            db.reorder_rules,
        )
        query = (
            select(
                rule,
                item.c.sku.label("item_sku"),
                item.c.name.label("item_name"),
                location.c.code.label("location_code"),
                vendor.c.code.label("vendor_code"),
            )
            .select_from(
                rule.join(
                    item,
                    (item.c.tenant_id == rule.c.tenant_id) & (item.c.item_id == rule.c.item_id),
                )
                .join(
                    location,
                    (location.c.tenant_id == rule.c.tenant_id)
                    & (location.c.location_id == rule.c.location_id),
                )
                .outerjoin(
                    vendor,
                    (vendor.c.tenant_id == rule.c.tenant_id)
                    & (vendor.c.vendor_id == rule.c.preferred_vendor_id),
                )
            )
            .where(self.scope(rule))
        )
        count = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self.session.execute(
            query.order_by(item.c.sku, location.c.code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [dict(row) for row in rows.mappings()],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def save_rule(self, payload, rule_id=None):
        self.authorize()
        await self.catalog.lock()
        item = await self.catalog.reference(catalog_db.items, "item_id", payload.item_id)
        kind = await self.catalog.reference(
            catalog_db.item_types, "item_type_id", item["item_type_id"]
        )
        if (
            item["status"] != "ACTIVE"
            or item["item_kind"] == "COMPOSITE"
            or not kind["is_stockable"]
        ):
            raise ApiError(422, "reorder_item_invalid", "Choose an active stockable component")
        location = await self.catalog.reference(
            inventory_db.locations, "location_id", payload.location_id
        )
        location_type = await self.catalog.row(
            inventory_db.location_types, "location_type_id", location["location_type_id"]
        )
        if (
            location_type["code"].upper() not in {"WAREHOUSE", "GODOWN", "STORE"}
            or not location_type["is_storage"]
        ):
            raise ApiError(422, "reorder_location_invalid", "Choose a sellable storage location")
        if payload.preferred_vendor_id:
            await self.catalog.reference(db.vendors, "vendor_id", payload.preferred_vendor_id)
        values = payload.model_dump()
        if rule_id:
            await self.catalog.row(db.reorder_rules, "reorder_rule_id", rule_id, lock=True)
            result = await self.session.execute(
                update(db.reorder_rules)
                .where(self.scope(db.reorder_rules), db.reorder_rules.c.reorder_rule_id == rule_id)
                .values(**values)
                .returning(db.reorder_rules)
            )
        else:
            result = await self.session.execute(
                insert(db.reorder_rules)
                .values(reorder_rule_id=uuid.uuid4(), tenant_id=self.context.tenant_id, **values)
                .returning(db.reorder_rules)
            )
        row = dict(result.mappings().one())
        vendor_code = None
        if payload.preferred_vendor_id:
            vendor_code = (
                await self.catalog.row(db.vendors, "vendor_id", payload.preferred_vendor_id)
            )["code"]
        return {
            **row,
            "item_sku": item["sku"],
            "item_name": item["name"],
            "location_code": location["code"],
            "vendor_code": vendor_code,
        }

    async def suggestions(self):
        rows = (await self.rules(1, 10000))["items"]
        groups = {}
        for rule in rows:
            if not rule["is_active"]:
                continue
            item_id, location_id = rule["item_id"], rule["location_id"]
            balance = (
                (
                    await self.session.execute(
                        select(inventory_db.balances).where(
                            self.scope(inventory_db.balances),
                            inventory_db.balances.c.item_id == item_id,
                            inventory_db.balances.c.location_id == location_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            item = await self.catalog.row(catalog_db.items, "item_id", item_id)
            open_lines = (
                await self.session.execute(
                    select(
                        db.purchase_order_lines.c.quantity,
                        db.purchase_order_lines.c.received_quantity,
                        db.purchase_order_lines.c.cancelled_quantity,
                        db.purchase_order_lines.c.quantity_in_stock_uom,
                    )
                    .select_from(
                        db.purchase_order_lines.join(
                            db.purchase_orders,
                            (db.purchase_orders.c.tenant_id == db.purchase_order_lines.c.tenant_id)
                            & (
                                db.purchase_orders.c.purchase_order_id
                                == db.purchase_order_lines.c.purchase_order_id
                            ),
                        )
                    )
                    .where(
                        self.scope(db.purchase_order_lines),
                        db.purchase_order_lines.c.item_id == item_id,
                        db.purchase_orders.c.location_id == location_id,
                        db.purchase_orders.c.status.in_(["APPROVED", "SENT", "PARTIALLY_RECEIVED"]),
                    )
                )
            ).all()
            outstanding = (
                sum(
                    (line.quantity - line.received_quantity - line.cancelled_quantity)
                    * line.quantity_in_stock_uom
                    / line.quantity
                    for line in open_lines
                )
                if open_lines
                else Decimal(0)
            )
            projected = (
                (balance["physical_on_hand"] - balance["committed"] if balance else Decimal(0))
                + outstanding
                - item["safety_stock"]
            )
            if rule["method"] == "REORDER_POINT":
                qty = rule["reorder_qty"] if projected <= rule["reorder_point"] else Decimal(0)
            elif rule["method"] == "MIN_MAX":
                qty = (
                    max(Decimal(0), rule["max_level"] - projected)
                    if projected <= rule["reorder_point"]
                    else Decimal(0)
                )
            else:
                qty = max(Decimal(0), rule["par_level"] - projected)
            if qty <= 0:
                continue
            vendor_id = rule["preferred_vendor_id"]
            if not vendor_id:
                vendor_id = await self.session.scalar(
                    select(db.item_vendors.c.vendor_id).where(
                        self.scope(db.item_vendors),
                        db.item_vendors.c.item_id == item_id,
                        db.item_vendors.c.is_preferred.is_(True),
                    )
                )
            vendor_name = (
                (await self.catalog.row(db.vendors, "vendor_id", vendor_id))["name"]
                if vendor_id
                else None
            )
            group = groups.setdefault(
                vendor_id,
                {
                    "vendor_id": vendor_id,
                    "vendor_name": vendor_name or "Unassigned supplier",
                    "items": [],
                },
            )
            group["items"].append(
                {
                    "item_id": item_id,
                    "item_sku": item["sku"],
                    "location_id": location_id,
                    "location_code": rule["location_code"],
                    "vendor_id": vendor_id,
                    "vendor_name": vendor_name,
                    "projected_available": projected,
                    "outstanding_order_qty": outstanding,
                    "suggested_qty": qty,
                    "method": rule["method"],
                }
            )
        return sorted(groups.values(), key=lambda group: group["vendor_name"])
