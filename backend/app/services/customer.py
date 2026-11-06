"""Customer and address setup; no sales reservation is created here."""

import csv
import io
import uuid

from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from app.core.errors import ApiError
from app.models import customer as db
from app.schemas.catalog import ImportIssue
from app.schemas.customer import CustomerImportPreview, CustomerIn
from app.services.catalog import CatalogService

CUSTOMER_COLUMNS = [
    "code",
    "name",
    "party_type",
    "gstin",
    "email",
    "phone",
    "tax_treatment",
    "place_of_supply_state",
    "payment_terms_days",
    "credit_limit",
]


class CustomerService:
    def __init__(self, session, context):
        self.session, self.context = session, context
        self.catalog = CatalogService(session, context)

    def scope(self, table):
        return table.c.tenant_id == self.context.tenant_id

    def authorize(self):
        if not self.context.can_write:
            raise ApiError(
                403, "customer_write_forbidden", "Only an Owner or Admin can edit customers"
            )

    async def customers(self, page, page_size, search=""):
        conditions = [self.scope(db.customers)]
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(
                db.customers.c.name.ilike(f"%{escaped}%", escape="\\")
                | db.customers.c.code.ilike(f"%{escaped}%", escape="\\")
            )
        count = await self.session.scalar(
            select(func.count()).select_from(db.customers).where(*conditions)
        )
        rows = await self.session.execute(
            select(db.customers)
            .where(*conditions)
            .order_by(db.customers.c.name, db.customers.c.customer_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [dict(row) for row in rows.mappings()],
            "total": count,
            "page": page,
            "page_size": page_size,
        }

    async def save(self, payload, customer_id=None):
        self.authorize()
        await self.catalog.lock()
        values = payload.model_dump(exclude={"expected_updated_at"})
        if customer_id:
            previous = await self.catalog.row(db.customers, "customer_id", customer_id, lock=True)
            if previous["updated_at"] != payload.expected_updated_at:
                raise ApiError(409, "stale_customer", "Reload this customer before saving")
        duplicate = await self.session.scalar(
            select(db.customers.c.customer_id).where(
                self.scope(db.customers),
                func.lower(db.customers.c.code) == values["code"].lower(),
                db.customers.c.customer_id != (customer_id or uuid.UUID(int=0)),
            )
        )
        if duplicate:
            raise ApiError(409, "customer_code_exists", "This customer code already exists")
        if customer_id:
            result = await self.session.execute(
                update(db.customers)
                .where(
                    self.scope(db.customers),
                    db.customers.c.customer_id == customer_id,
                )
                .values(**values)
                .returning(db.customers)
            )
        else:
            result = await self.session.execute(
                insert(db.customers)
                .values(
                    customer_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    **values,
                )
                .returning(db.customers)
            )
        return dict(result.mappings().one())

    async def addresses(self, customer_id):
        await self.catalog.row(db.customers, "customer_id", customer_id)
        rows = await self.session.execute(
            select(db.addresses)
            .where(
                self.scope(db.addresses),
                db.addresses.c.customer_id == customer_id,
            )
            .order_by(db.addresses.c.address_type, db.addresses.c.is_default.desc())
        )
        return [dict(row) for row in rows.mappings()]

    async def add_address(self, customer_id, payload):
        self.authorize()
        await self.catalog.lock()
        await self.catalog.row(db.customers, "customer_id", customer_id, lock=True)
        if payload.is_default:
            await self.session.execute(
                update(db.addresses)
                .where(
                    self.scope(db.addresses),
                    db.addresses.c.customer_id == customer_id,
                    db.addresses.c.address_type == payload.address_type,
                )
                .values(is_default=False)
            )
        row = await self.session.execute(
            insert(db.addresses)
            .values(
                address_id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                customer_id=customer_id,
                **payload.model_dump(),
            )
            .returning(db.addresses)
        )
        return dict(row.mappings().one())

    async def validate_csv(self, source):
        if "\x00" in source:
            raise ApiError(422, "customer_csv_format", "Use the customer CSV template")
        try:
            reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")), strict=True)
            if reader.fieldnames != CUSTOMER_COLUMNS:
                raise ApiError(422, "customer_csv_format", "Use the ten customer CSV columns")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ApiError(422, "customer_csv_format", "Every CSV row needs ten cells")
                if any(value.strip() for value in row.values()):
                    rows.append({key: value.strip() for key, value in row.items()})
                if len(rows) > 500:
                    raise ApiError(422, "customer_csv_limit", "Use at most 500 customer rows")
        except csv.Error:
            raise ApiError(
                422, "customer_csv_format", "The customer CSV could not be read"
            ) from None
        if not rows:
            raise ApiError(422, "customer_csv_empty", "Provide at least one customer row")
        existing = (
            await self.session.execute(select(db.customers.c.code).where(self.scope(db.customers)))
        ).scalars()
        seen = {code.casefold() for code in existing}
        parsed, issues = [], []
        for number, row in enumerate(rows, 2):
            code = row["code"].casefold()
            if code in seen:
                issues.append(
                    ImportIssue(row=number, field="code", message="Customer code already exists")
                )
                continue
            seen.add(code)
            try:
                payload = CustomerIn(**{key: value for key, value in row.items() if value})
                parsed.append(payload)
            except ValidationError as exc:
                issues.extend(
                    ImportIssue(row=number, field=str(error["loc"][0]), message=error["msg"])
                    for error in exc.errors(include_input=False, include_context=False)
                )
        return CustomerImportPreview(
            row_count=len(rows),
            valid_count=len(parsed),
            can_import=not issues,
            issues=issues,
            rows=parsed,
        )

    async def import_csv(self, source):
        self.authorize()
        await self.catalog.lock()
        preview = await self.validate_csv(source)
        if not preview.can_import:
            raise ApiError(422, "customer_csv_errors", "Correct the customer CSV errors")
        for customer in preview.rows:
            await self.session.execute(
                insert(db.customers).values(
                    customer_id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    **customer.model_dump(),
                )
            )
        return preview


def customer_template():
    return (
        "code,name,party_type,gstin,email,phone,tax_treatment,place_of_supply_state,"
        "payment_terms_days,credit_limit\n"
        "BUYER-001,Example buyer,B2B,,buyer@example.com,,REGISTERED,27,30,50000\n"
    )
