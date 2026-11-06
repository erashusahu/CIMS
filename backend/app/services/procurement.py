"""Tenant-scoped supplier setup and CSV preview for the first procurement increment."""

import csv
import io
import uuid

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import procurement as db
from app.schemas.catalog import ImportIssue
from app.schemas.procurement import SupplierImportPreview, SupplierIn
from app.services.catalog import CatalogService
from pydantic import ValidationError
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

SUPPLIER_COLUMNS = [
    "code",
    "name",
    "gstin",
    "email",
    "phone",
    "lead_time_days",
    "payment_terms_days",
]


class ProcurementService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    def authorize(self):
        if not self.context.can_write:
            raise ApiError(
                403, "procurement_write_forbidden", "Only an Owner or Admin can edit suppliers"
            )

    async def suppliers(self, page, page_size, search=""):
        conditions = [self.scope(db.vendors)]
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(
                db.vendors.c.name.ilike(f"%{escaped}%", escape="\\")
                | db.vendors.c.code.ilike(f"%{escaped}%", escape="\\")
            )
        total = await self.session.scalar(
            select(func.count()).select_from(db.vendors).where(*conditions)
        )
        rows = await self.session.execute(
            select(db.vendors)
            .where(*conditions)
            .order_by(db.vendors.c.name, db.vendors.c.vendor_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [dict(row) for row in rows.mappings()],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def save_supplier(self, payload, vendor_id=None):
        self.authorize()
        await self.catalog.lock()
        values = payload.model_dump(exclude={"expected_updated_at"})
        previous = None
        if vendor_id:
            previous = await self.catalog.row(db.vendors, "vendor_id", vendor_id, lock=True)
            if previous["updated_at"] != payload.expected_updated_at:
                raise ApiError(
                    409, "stale_supplier", "This supplier changed. Reload it before saving"
                )
        duplicate = await self.session.scalar(
            select(db.vendors.c.vendor_id).where(
                self.scope(db.vendors),
                func.lower(db.vendors.c.code) == values["code"].lower(),
                db.vendors.c.vendor_id != (vendor_id or uuid.UUID(int=0)),
            )
        )
        if duplicate:
            raise ApiError(409, "supplier_code_exists", "This supplier code already exists")
        if previous:
            result = await self.session.execute(
                update(db.vendors)
                .where(
                    self.scope(db.vendors),
                    db.vendors.c.vendor_id == vendor_id,
                )
                .values(**values)
                .returning(db.vendors)
            )
        else:
            result = await self.session.execute(
                insert(db.vendors)
                .values(
                    vendor_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    **values,
                )
                .returning(db.vendors)
            )
        return dict(result.mappings().one())

    async def preferred_suppliers(self, page, page_size):
        item = catalog_db.items
        vendor = db.vendors
        link = db.item_vendors
        statement = (
            select(
                link.c.item_vendor_id,
                link.c.item_id,
                link.c.vendor_id,
                link.c.vendor_item_code,
                link.c.unit_price,
                link.c.min_order_qty,
                link.c.lead_time_days,
                item.c.sku.label("item_sku"),
                vendor.c.code.label("vendor_code"),
                vendor.c.name.label("vendor_name"),
            )
            .select_from(
                link.join(
                    item,
                    (item.c.tenant_id == link.c.tenant_id) & (item.c.item_id == link.c.item_id),
                ).join(
                    vendor,
                    (vendor.c.tenant_id == link.c.tenant_id)
                    & (vendor.c.vendor_id == link.c.vendor_id),
                )
            )
            .where(
                self.scope(link),
                link.c.is_preferred.is_(True),
            )
        )
        total = await self.session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self.session.execute(
            statement.order_by(item.c.sku, vendor.c.code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [dict(row) for row in rows.mappings()],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def set_preferred_supplier(self, payload):
        self.authorize()
        await self.catalog.lock()
        item = await self.catalog.reference(catalog_db.items, "item_id", payload.item_id)
        kind = await self.catalog.reference(
            catalog_db.item_types, "item_type_id", item["item_type_id"]
        )
        if (
            item["item_kind"] == "COMPOSITE"
            or item["status"] != "ACTIVE"
            or not kind["is_stockable"]
        ):
            raise ApiError(422, "supplier_item_not_stockable", "Choose a stockable catalog item")
        vendor = await self.catalog.reference(db.vendors, "vendor_id", payload.vendor_id)
        await self.session.execute(
            update(db.item_vendors)
            .where(
                self.scope(db.item_vendors),
                db.item_vendors.c.item_id == payload.item_id,
                db.item_vendors.c.is_preferred.is_(True),
            )
            .values(is_preferred=False)
        )
        statement = (
            pg_insert(db.item_vendors)
            .values(
                item_vendor_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                item_id=payload.item_id,
                vendor_id=payload.vendor_id,
                vendor_item_code=payload.vendor_item_code,
                unit_price=payload.unit_price,
                min_order_qty=payload.min_order_qty,
                lead_time_days=payload.lead_time_days,
                currency="INR",
                is_preferred=True,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "item_id", "vendor_id"],
                set_={
                    "vendor_item_code": payload.vendor_item_code,
                    "unit_price": payload.unit_price,
                    "min_order_qty": payload.min_order_qty,
                    "lead_time_days": payload.lead_time_days,
                    "is_preferred": True,
                },
            )
            .returning(db.item_vendors)
        )
        link = (await self.session.execute(statement)).mappings().one()
        return {
            **dict(link),
            "item_sku": item["sku"],
            "vendor_code": vendor["code"],
            "vendor_name": vendor["name"],
        }

    async def validate_supplier_csv(self, source):
        if "\x00" in source:
            raise ApiError(422, "supplier_csv_format", "Use the supplier CSV template")
        try:
            reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")), strict=True)
            if reader.fieldnames != SUPPLIER_COLUMNS:
                raise ApiError(422, "supplier_csv_format", "Use the seven supplier CSV columns")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ApiError(
                        422, "supplier_csv_format", "Every supplier CSV row needs seven cells"
                    )
                if any(value.strip() for value in row.values()):
                    rows.append({key: value.strip() for key, value in row.items()})
                if len(rows) > 500:
                    raise ApiError(422, "supplier_csv_limit", "Use at most 500 supplier rows")
        except csv.Error:
            raise ApiError(
                422, "supplier_csv_format", "The supplier CSV could not be read"
            ) from None
        if not rows:
            raise ApiError(422, "supplier_csv_empty", "Provide at least one supplier row")
        existing = set(
            (
                await self.session.execute(
                    select(db.vendors.c.code).where(
                        self.scope(db.vendors),
                    )
                )
            ).scalars()
        )
        seen = {code.casefold() for code in existing}
        parsed, issues = [], []
        for number, row in enumerate(rows, start=2):
            try:
                code = row["code"].casefold()
                if code in seen:
                    raise ApiError(409, "supplier_code_exists", "Supplier code already exists")
                seen.add(code)
                payload = SupplierIn(
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {"name", "code"} and value
                    },
                    code=row["code"],
                    name=row["name"],
                )
                parsed.append(payload)
            except ApiError as exc:
                issues.append(ImportIssue(row=number, field="code", message=exc.message))
            except ValidationError as exc:
                issues.extend(
                    ImportIssue(row=number, field=str(error["loc"][0]), message=error["msg"])
                    for error in exc.errors(include_input=False, include_context=False)
                )
        return SupplierImportPreview(
            row_count=len(rows),
            valid_count=len(parsed),
            can_import=not issues,
            issues=issues,
            rows=parsed,
        )

    async def import_suppliers(self, source):
        self.authorize()
        await self.catalog.lock()
        preview = await self.validate_supplier_csv(source)
        if not preview.can_import:
            raise ApiError(422, "supplier_csv_errors", "Correct the supplier CSV errors")
        for supplier in preview.rows:
            await self.session.execute(
                insert(db.vendors).values(
                    vendor_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    **supplier.model_dump(),
                )
            )
        return preview


def supplier_template():
    return (
        "code,name,gstin,email,phone,lead_time_days,payment_terms_days\n"
        "SUP-001,Example supplier,,orders@example.com,,7,30\n"
    )
