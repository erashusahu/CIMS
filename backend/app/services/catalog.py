import uuid
from collections import defaultdict
from decimal import Decimal

from app.api.catalog_access import CatalogContext
from app.core.errors import ApiError
from app.models import catalog as db
from app.schemas.catalog import (
    ComponentIn,
    GroupIn,
    ImportIssue,
    ImportPreview,
    ItemIn,
    ItemTypeIn,
    ItemUpdate,
    UnitIn,
)
from app.services.catalog_rules import assert_acyclic, exact_stock_quantity, parse_item_csv
from pydantic import ValidationError
from sqlalchemy import Table, delete, exists, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession


class CatalogService:
    def __init__(self, session: AsyncSession, context: CatalogContext):
        self.session = session
        self.context = context

    @property
    def tenant_id(self):
        return self.context.tenant_id

    def scope(self, table: Table):
        return table.c.tenant_id == self.tenant_id

    async def prepare(self) -> None:
        # Existing composite/audit triggers resolve their internal table names via search_path.
        # Both settings are transaction-local and reset when the session commits/rolls back.
        await self.session.execute(text("SET LOCAL search_path = cims, pg_catalog, public"))
        await self.session.execute(
            text(
                "SELECT set_config('app.tenant_id', :tenant, true), "
                "set_config('app.user_id', :user, true), "
                "set_config('app.request_id', :request, true), "
                "set_config('app.actor_type', :actor, true)"
            ),
            {
                "tenant": str(self.tenant_id),
                "user": str(self.context.user_id or ""),
                "request": self.context.request_id,
                "actor": "SYSTEM" if self.context.user_id is None else "USER",
            },
        )

    async def lock(self) -> None:
        if not self.context.can_write:
            raise ApiError(
                403, "catalog_write_forbidden", "Only an Owner or Admin can change the catalog"
            )
        # Serialize catalog graph/import edits per business. This prevents concurrent kit
        # changes from each validating against an outdated graph, including an empty graph.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"cims.catalog:{self.tenant_id}"},
        )

    async def row(self, table: Table, key: str, value: uuid.UUID, *, lock=False):
        statement = select(table).where(self.scope(table), table.c[key] == value)
        if lock:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).mappings().first()
        if row is None:
            raise ApiError(404, "catalog_record_not_found", "The catalog record is unavailable")
        return dict(row)

    async def reference(self, table: Table, key: str, value: uuid.UUID | None):
        if value is None:
            return None
        try:
            row = await self.row(table, key, value)
        except ApiError:
            raise ApiError(
                422, "invalid_catalog_reference", "Choose a record from this business workspace"
            ) from None
        if row.get("is_active") is False or row.get("status") == "INACTIVE":
            raise ApiError(422, "inactive_catalog_reference", "Choose an active catalog record")
        return row

    async def page(
        self, table: Table, key: str, page: int, page_size: int, *, search="", status=None
    ):
        conditions = [self.scope(table)]
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            matches = [table.c.name.ilike(f"%{escaped}%", escape="\\")]
            if "sku" in table.c:
                matches.extend(
                    [
                        table.c.sku.ilike(f"%{escaped}%", escape="\\"),
                        table.c.barcode.ilike(f"%{escaped}%", escape="\\"),
                    ]
                )
            conditions.append(or_(*matches))
        if status:
            conditions.append(table.c.status == status)
        total = await self.session.scalar(
            select(func.count()).select_from(table).where(*conditions)
        )
        rows = (
            (
                await self.session.execute(
                    select(table)
                    .where(*conditions)
                    .order_by(func.lower(table.c.name), table.c[key])
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def options(self):
        result = {"can_write": self.context.can_write, "local_mode": self.context.local}
        for name, table in [
            ("units", db.units),
            ("item_types", db.item_types),
            ("groups", db.groups),
            ("hsn_codes", db.hsn_codes),
            ("cost_centers", db.cost_centers),
        ]:
            order = table.c.name if "name" in table.c else table.c.code
            rows = (
                (await self.session.execute(select(table).where(self.scope(table)).order_by(order)))
                .mappings()
                .all()
            )
            result[name] = [dict(row) for row in rows]
        for group in result["groups"]:
            group["attributes"] = await self.group_names(group["item_group_id"])
        result["default_valuation_method"] = await self.session.scalar(
            select(db.tenants.c.default_valuation_method).where(self.scope(db.tenants))
        )
        return result

    async def audit(
        self, table_name: str, record_id: uuid.UUID, values: dict, *, action="CREATE"
    ) -> None:
        # Item, conversion and kit tables already have database audit triggers. These
        # setup tables do not; record their sanitized, non-credential payloads explicitly.
        await self.session.execute(
            insert(db.audit_log).values(
                tenant_id=self.tenant_id,
                actor_user_id=self.context.user_id,
                action=action,
                entity_table=table_name,
                entity_pk=str(record_id),
                new_value=values,
                request_id=self.context.request_id,
            )
        )

    async def create_unit(self, payload: UnitIn):
        await self.lock()
        row = (
            (
                await self.session.execute(
                    insert(db.units)
                    .values(
                        tenant_id=self.tenant_id,
                        **payload.model_dump(),
                        is_system=False,
                    )
                    .returning(db.units)
                )
            )
            .mappings()
            .one()
        )
        await self.audit("uom", row["uom_id"], payload.model_dump(mode="json"))
        return dict(row)

    async def update_unit(self, unit_id: uuid.UUID, payload: UnitIn):
        await self.lock()
        old = await self.row(db.units, "uom_id", unit_id, lock=True)
        if old["is_system"] and payload.code != old["code"]:
            raise ApiError(422, "system_unit", "A system unit's code cannot be changed")
        dimensional_change = (
            payload.decimal_places != old["decimal_places"] or payload.uom_class != old["uom_class"]
        )
        used = await self.session.scalar(
            select(
                or_(
                    exists(
                        select(1)
                        .select_from(db.items)
                        .where(
                            self.scope(db.items),
                            or_(
                                db.items.c.stock_uom_id == unit_id,
                                db.items.c.purchase_uom_id == unit_id,
                                db.items.c.sales_uom_id == unit_id,
                            ),
                        )
                    ),
                    exists(
                        select(1)
                        .select_from(db.conversions)
                        .where(
                            self.scope(db.conversions),
                            or_(
                                db.conversions.c.from_uom_id == unit_id,
                                db.conversions.c.to_uom_id == unit_id,
                            ),
                        )
                    ),
                    exists(
                        select(1)
                        .select_from(db.components)
                        .where(self.scope(db.components), db.components.c.uom_id == unit_id)
                    ),
                )
            )
        )
        if used and (dimensional_change or not payload.is_active):
            raise ApiError(
                409,
                "unit_in_use",
                "An assigned unit cannot be deactivated or change precision/class",
            )
        row = (
            (
                await self.session.execute(
                    update(db.units)
                    .where(self.scope(db.units), db.units.c.uom_id == unit_id)
                    .values(**payload.model_dump())
                    .returning(db.units)
                )
            )
            .mappings()
            .one()
        )
        await self.audit("uom", unit_id, payload.model_dump(mode="json"), action="UPDATE")
        return dict(row)

    async def create_type(self, payload: ItemTypeIn):
        await self.lock()
        row = (
            (
                await self.session.execute(
                    insert(db.item_types)
                    .values(
                        tenant_id=self.tenant_id,
                        **payload.model_dump(),
                        is_system=False,
                    )
                    .returning(db.item_types)
                )
            )
            .mappings()
            .one()
        )
        await self.audit("item_type", row["item_type_id"], payload.model_dump(mode="json"))
        return dict(row)

    async def group_names(self, group_id: uuid.UUID):
        return list(
            (
                await self.session.execute(
                    select(db.group_attributes.c.name)
                    .where(
                        self.scope(db.group_attributes),
                        db.group_attributes.c.item_group_id == group_id,
                    )
                    .order_by(db.group_attributes.c.sort_order, db.group_attributes.c.attribute_id)
                )
            ).scalars()
        )

    async def create_group(self, payload: GroupIn):
        await self.lock()
        await self.reference(db.item_types, "item_type_id", payload.default_item_type_id)
        row = dict(
            (
                await self.session.execute(
                    insert(db.groups)
                    .values(
                        tenant_id=self.tenant_id,
                        **payload.model_dump(exclude={"attributes"}),
                        is_active=True,
                    )
                    .returning(db.groups)
                )
            )
            .mappings()
            .one()
        )
        for order, name in enumerate(payload.attributes):
            await self.session.execute(
                insert(db.group_attributes).values(
                    tenant_id=self.tenant_id,
                    item_group_id=row["item_group_id"],
                    name=name,
                    sort_order=order,
                )
            )
        row["attributes"] = payload.attributes
        await self.audit("item_group", row["item_group_id"], payload.model_dump(mode="json"))
        return row

    async def item_details(self, item_id: uuid.UUID):
        row = await self.row(db.items, "item_id", item_id)
        row["conversions"] = [
            dict(entry)
            for entry in (
                await self.session.execute(
                    select(db.conversions.c.from_uom_id, db.conversions.c.factor)
                    .where(
                        self.scope(db.conversions),
                        db.conversions.c.item_id == item_id,
                        db.conversions.c.to_uom_id == row["stock_uom_id"],
                    )
                    .order_by(db.conversions.c.from_uom_id)
                )
            ).mappings()
        ]
        row["components"] = [
            dict(entry)
            for entry in (
                await self.session.execute(
                    select(
                        db.components.c.component_item_id,
                        db.components.c.quantity,
                        db.components.c.uom_id,
                    )
                    .where(self.scope(db.components), db.components.c.kit_item_id == item_id)
                    .order_by(db.components.c.sort_order, db.components.c.component_item_id)
                )
            ).mappings()
        ]
        row["attributes"] = dict(
            (
                await self.session.execute(
                    select(db.group_attributes.c.name, db.attribute_values.c.value)
                    .join(
                        db.attribute_values,
                        db.attribute_values.c.attribute_id == db.group_attributes.c.attribute_id,
                    )
                    .where(
                        self.scope(db.group_attributes),
                        self.scope(db.attribute_values),
                        db.attribute_values.c.item_id == item_id,
                    )
                )
            ).all()
        )
        return row

    async def factor_for(self, item: dict, unit_id: uuid.UUID) -> Decimal:
        if unit_id == item["stock_uom_id"]:
            return Decimal(1)
        factor = await self.session.scalar(
            select(db.conversions.c.factor).where(
                self.scope(db.conversions),
                db.conversions.c.item_id == item["item_id"],
                db.conversions.c.from_uom_id == unit_id,
                db.conversions.c.to_uom_id == item["stock_uom_id"],
            )
        )
        if factor is None:
            raise ApiError(
                422, "conversion_missing", "Provide an item-specific conversion to the stock unit"
            )
        return factor

    async def convert(self, item_id: uuid.UUID, from_unit: uuid.UUID, quantity: Decimal):
        item = await self.row(db.items, "item_id", item_id)
        source = await self.reference(db.units, "uom_id", from_unit)
        target = await self.reference(db.units, "uom_id", item["stock_uom_id"])
        exact_stock_quantity(quantity, Decimal(1), source["decimal_places"])
        stock_quantity = exact_stock_quantity(
            quantity, await self.factor_for(item, from_unit), target["decimal_places"]
        )
        return {
            "item_id": item_id,
            "stock_uom_id": item["stock_uom_id"],
            "stock_quantity": stock_quantity,
        }

    async def validate_item(self, payload: ItemIn, item_id: uuid.UUID):
        stock = await self.reference(db.units, "uom_id", payload.stock_uom_id)
        if stock["decimal_places"] > 4:
            raise ApiError(
                422,
                "stock_precision",
                "The current catalog supports up to four stock-unit decimal places",
            )
        await self.reference(db.item_types, "item_type_id", payload.item_type_id)
        await self.reference(db.hsn_codes, "hsn_sac_id", payload.hsn_sac_id)
        await self.reference(db.cost_centers, "cost_center_id", payload.cost_center_id)
        provided = {entry.from_uom_id: entry.factor for entry in payload.conversions}
        for unit_id in {payload.purchase_uom_id, payload.sales_uom_id} - {
            None,
            payload.stock_uom_id,
        }:
            if unit_id not in provided:
                raise ApiError(
                    422,
                    "conversion_missing",
                    "Purchase and sales units require conversions to the stock unit",
                )
        for entry in payload.conversions:
            await self.reference(db.units, "uom_id", entry.from_uom_id)
            exact_stock_quantity(Decimal(1), entry.factor, stock["decimal_places"])
        if payload.safety_stock:
            exact_stock_quantity(payload.safety_stock, Decimal(1), stock["decimal_places"])
        if payload.item_group_id:
            await self.reference(db.groups, "item_group_id", payload.item_group_id)
            names = await self.group_names(payload.item_group_id)
            if not names or set(names) != set(payload.attributes):
                raise ApiError(
                    422,
                    "variant_attributes",
                    "Provide every attribute defined by the variant group",
                )
        if payload.components:
            rows = (
                await self.session.execute(
                    select(db.components.c.kit_item_id, db.components.c.component_item_id).where(
                        self.scope(db.components)
                    )
                )
            ).all()
            edges = defaultdict(set)
            for parent, child in rows:
                edges[parent].add(child)
            assert_acyclic(
                item_id, [entry.component_item_id for entry in payload.components], edges
            )
            for entry in payload.components:
                await self.validate_component(entry)

    async def validate_component(self, entry: ComponentIn):
        item = await self.reference(db.items, "item_id", entry.component_item_id)
        if item["status"] != "ACTIVE":
            raise ApiError(422, "inactive_component", "Kit components must be active")
        source = await self.reference(db.units, "uom_id", entry.uom_id)
        target = await self.reference(db.units, "uom_id", item["stock_uom_id"])
        exact_stock_quantity(entry.quantity, Decimal(1), source["decimal_places"])
        exact_stock_quantity(
            entry.quantity, await self.factor_for(item, entry.uom_id), target["decimal_places"]
        )

    async def save_item(self, payload: ItemIn, item_id: uuid.UUID | None = None, *, locked=False):
        if not locked:
            await self.lock()
        old = None
        if item_id is not None:
            old = await self.row(db.items, "item_id", item_id, lock=True)
            if (
                not isinstance(payload, ItemUpdate)
                or payload.expected_version != old["catalog_version"]
            ):
                raise ApiError(
                    409, "stale_catalog_item", "This item changed. Reload it before saving"
                )
            if old["item_kind"] != payload.item_kind:
                raise ApiError(
                    422, "item_kind_locked", "Create another item to use a different item kind"
                )
            if (
                old["stock_uom_id"] != payload.stock_uom_id
                or old["valuation_method"] != payload.valuation_method
            ):
                moved = old["valuation_locked_at"] is not None or await self.session.scalar(
                    select(
                        exists(
                            select(1)
                            .select_from(db.ledger)
                            .where(self.scope(db.ledger), db.ledger.c.item_id == item_id)
                        )
                    )
                )
                if moved:
                    raise ApiError(
                        409,
                        "valuation_locked",
                        "Stock unit and valuation cannot change after stock movements",
                    )
                referenced = await self.session.scalar(
                    select(
                        exists(
                            select(1)
                            .select_from(db.components)
                            .where(
                                self.scope(db.components),
                                db.components.c.component_item_id == item_id,
                            )
                        )
                    )
                )
                if referenced and old["stock_uom_id"] != payload.stock_uom_id:
                    raise ApiError(
                        409,
                        "kit_component_unit_locked",
                        "Remove this item from kits before changing its stock unit",
                    )
        item_id = item_id or uuid.uuid4()
        await self.validate_item(payload, item_id)
        values = payload.model_dump(
            exclude={"conversions", "components", "attributes", "expected_version"}
        )
        if old:
            await self.session.execute(
                update(db.items)
                .where(
                    self.scope(db.items),
                    db.items.c.item_id == item_id,
                )
                .values(**values, catalog_version=old["catalog_version"] + 1)
            )
            await self.session.execute(
                delete(db.conversions).where(
                    self.scope(db.conversions),
                    db.conversions.c.item_id == item_id,
                    db.conversions.c.to_uom_id == old["stock_uom_id"],
                )
            )
            await self.session.execute(
                delete(db.components).where(
                    self.scope(db.components), db.components.c.kit_item_id == item_id
                )
            )
            await self.session.execute(
                delete(db.attribute_values).where(
                    self.scope(db.attribute_values), db.attribute_values.c.item_id == item_id
                )
            )
        else:
            await self.session.execute(
                insert(db.items).values(
                    item_id=item_id,
                    tenant_id=self.tenant_id,
                    created_by=self.context.user_id,
                    **values,
                    catalog_version=1,
                )
            )
        for entry in payload.conversions:
            await self.session.execute(
                insert(db.conversions).values(
                    tenant_id=self.tenant_id,
                    item_id=item_id,
                    from_uom_id=entry.from_uom_id,
                    to_uom_id=payload.stock_uom_id,
                    factor=entry.factor,
                    rounding_mode="HALF_UP",
                )
            )
        for order, entry in enumerate(payload.components):
            await self.session.execute(
                insert(db.components).values(
                    tenant_id=self.tenant_id,
                    kit_item_id=item_id,
                    **entry.model_dump(),
                    sort_order=order,
                )
            )
        if payload.attributes:
            attributes = (
                await self.session.execute(
                    select(db.group_attributes).where(
                        self.scope(db.group_attributes),
                        db.group_attributes.c.item_group_id == payload.item_group_id,
                    )
                )
            ).mappings()
            for attribute in attributes:
                await self.session.execute(
                    insert(db.attribute_values).values(
                        tenant_id=self.tenant_id,
                        item_id=item_id,
                        attribute_id=attribute["attribute_id"],
                        value=payload.attributes[attribute["name"]],
                    )
                )
        return await self.item_details(item_id)

    async def csv_payload(self, row: dict[str, str], options: dict) -> ItemIn:
        def find(collection: str, code: str, key: str):
            match = next(
                (
                    value
                    for value in options[collection]
                    if value["code"].casefold() == code.casefold()
                ),
                None,
            )
            if not match:
                raise ApiError(
                    422, "csv_reference", f"Unknown {collection.replace('_', ' ')} code: {code}"
                )
            return match[key]

        stock = find("units", row["stock_unit"], "uom_id")
        conversion_values = {}
        selected = {}
        for kind in ["purchase", "sales"]:
            code = row.get(f"{kind}_unit") or row["stock_unit"]
            unit_id = find("units", code, "uom_id")
            selected[f"{kind}_uom_id"] = unit_id
            factor_text = row.get(f"{kind}_factor", "")
            if unit_id != stock:
                if not factor_text:
                    raise ApiError(
                        422, "csv_conversion", f"Provide {kind}_factor for the selected unit"
                    )
                factor = Decimal(factor_text)
                if unit_id in conversion_values and conversion_values[unit_id] != factor:
                    raise ApiError(
                        422, "csv_conversion", "The same unit cannot have different factors"
                    )
                conversion_values[unit_id] = factor
            elif factor_text and Decimal(factor_text) != 1:
                raise ApiError(422, "csv_conversion", "The stock unit's factor must be one")
        returnable = (row.get("returnable") or "true").lower()
        if returnable not in {"true", "false"}:
            raise ApiError(422, "csv_boolean", "Returnable must be true or false")
        values = {
            field: row[field]
            for field in [
                "cost_price",
                "selling_price",
                "gst_rate",
                "safety_stock",
                "status",
                "valuation_method",
            ]
            if row.get(field)
        }
        return ItemIn(
            sku=row["sku"],
            name=row["name"],
            stock_uom_id=stock,
            item_type_id=find("item_types", row["item_type"], "item_type_id"),
            barcode=row.get("barcode") or None,
            hsn_sac_id=find("hsn_codes", row["hsn_code"], "hsn_sac_id")
            if row.get("hsn_code")
            else None,
            is_returnable=returnable == "true",
            **selected,
            **values,
            conversions=[
                {"from_uom_id": unit_id, "factor": factor}
                for unit_id, factor in conversion_values.items()
            ],
        )

    async def validate_csv(self, source: str):
        rows = parse_item_csv(source)
        options = await self.options()
        existing = (
            await self.session.execute(
                select(db.items.c.sku, db.items.c.barcode).where(
                    self.scope(db.items),
                    or_(
                        func.lower(db.items.c.sku).in_([row["sku"].lower() for row in rows]),
                        func.lower(db.items.c.barcode).in_(
                            [row.get("barcode", "").lower() for row in rows if row.get("barcode")]
                        ),
                    ),
                )
            )
        ).all()
        taken_skus = {sku.casefold() for sku, _ in existing}
        taken_barcodes = {barcode.casefold() for _, barcode in existing if barcode}
        seen_skus, seen_barcodes = set(), set()
        issues, valid = [], []
        for line, row in enumerate(rows, start=2):
            row_issues = []
            for field, seen, taken in [
                ("sku", seen_skus, taken_skus),
                ("barcode", seen_barcodes, taken_barcodes),
            ]:
                value = row.get(field, "").casefold()
                if value and (value in seen or value in taken):
                    row_issues.append(
                        ImportIssue(
                            row=line,
                            field=field,
                            message=f"Duplicate {field} in this business or CSV",
                        )
                    )
                if value:
                    seen.add(value)
            try:
                payload = await self.csv_payload(row, options)
                await self.validate_item(payload, uuid.uuid4())
            except ValidationError as exc:
                row_issues.extend(
                    ImportIssue(row=line, field=str(error["loc"][0]), message=error["msg"])
                    for error in exc.errors(include_input=False, include_context=False)
                )
            except (ValueError, ArithmeticError):
                row_issues.append(
                    ImportIssue(
                        row=line, field="number", message="Provide valid finite decimal numbers"
                    )
                )
            except ApiError as exc:
                row_issues.append(ImportIssue(row=line, field="row", message=exc.message))
            if not row_issues:
                valid.append(payload)
            issues.extend(row_issues)
        preview = ImportPreview(
            row_count=len(rows),
            valid_count=len(valid),
            issues=issues,
            can_import=not issues,
            rows=rows,
        )
        return preview, valid

    async def commit_csv(self, source: str):
        await self.lock()
        # Always validate again inside the posting transaction. A client-side preview
        # is not authority, and the unique constraints are the final concurrency guard.
        preview, payloads = await self.validate_csv(source)
        if not preview.can_import:
            raise ApiError(
                422,
                "csv_validation_failed",
                "The CSV has errors. Validate it again before importing",
            )
        result = []
        for payload in payloads:
            result.append(await self.save_item(payload, locked=True))
        return {"created": len(result), "item_ids": [row["item_id"] for row in result]}
