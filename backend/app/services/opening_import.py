import csv
import io

from app.core.errors import ApiError
from app.models import catalog as catalog_db
from app.models import inventory as db
from app.schemas.catalog import ImportIssue
from app.schemas.inventory import OpeningImportPreview, OpeningIn, OpeningLineIn
from pydantic import ValidationError
from sqlalchemy import select

COLUMNS = ["sku", "location", "unit", "quantity", "unit_cost"]


def opening_template():
    return "sku,location,unit,quantity,unit_cost\nSOAP,MAIN,PCS,10,100.00\n"


class OpeningImporter:
    def __init__(self, opening):
        self.opening = opening

    async def validate(self, source, business_date):
        try:
            reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")), strict=True)
            if reader.fieldnames != COLUMNS or "\x00" in source:
                raise ApiError(
                    422,
                    "opening_csv_format",
                    "Use the opening-stock CSV template and its five columns",
                )
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ApiError(
                        422, "opening_csv_format", "Every CSV row must contain five values"
                    )
                if any(value.strip() for value in row.values()):
                    rows.append({key: value.strip() for key, value in row.items()})
                if len(rows) > 100:
                    raise ApiError(
                        422, "opening_csv_limit", "Use at most 100 opening-stock lines per document"
                    )
        except csv.Error:
            raise ApiError(
                422, "opening_csv_format", "The CSV could not be read; check its quoting"
            ) from None
        if not rows:
            raise ApiError(422, "opening_csv_empty", "Provide at least one opening-stock row")
        maps = {}
        for name, table, key, code in [
            ("item", catalog_db.items, "item_id", "sku"),
            ("location", db.locations, "location_id", "code"),
            ("unit", catalog_db.units, "uom_id", "code"),
        ]:
            records = (
                await self.opening.session.execute(
                    select(table.c[key], table.c[code]).where(self.opening.scope(table))
                )
            ).all()
            maps[name] = {value.casefold(): identity for identity, value in records}
        issues, previews, payloads, seen = [], [], [], set()
        for number, row in enumerate(rows, start=2):
            try:
                item_id = maps["item"].get(row["sku"].casefold())
                location_id = maps["location"].get(row["location"].casefold())
                unit_id = maps["unit"].get(row["unit"].casefold())
                if not all([item_id, location_id, unit_id]):
                    raise ApiError(
                        422,
                        "opening_csv_reference",
                        "Choose existing item, location and unit codes from this workspace",
                    )
                if (item_id, location_id) in seen:
                    raise ApiError(
                        422, "opening_csv_duplicate", "Use one row per item and location"
                    )
                seen.add((item_id, location_id))
                line = OpeningLineIn(
                    item_id=item_id,
                    location_id=location_id,
                    uom_id=unit_id,
                    quantity=row["quantity"],
                    unit_cost=row["unit_cost"],
                )
                snapshots = await self.opening.snapshots(
                    OpeningIn(business_date=business_date, lines=[line])
                )
                snapshot = snapshots[0]
                previews.append(
                    {
                        **row,
                        "stock_quantity": str(snapshot["stock_quantity"]),
                        "stock_unit": snapshot["stock_unit_snapshot"],
                    }
                )
                payloads.append(line)
            except ValidationError as exc:
                issues.extend(
                    ImportIssue(row=number, field=str(error["loc"][0]), message=error["msg"])
                    for error in exc.errors(include_input=False, include_context=False)
                )
            except ApiError as exc:
                issues.append(ImportIssue(row=number, field="row", message=exc.message))
        preview = OpeningImportPreview(
            row_count=len(rows),
            valid_count=len(payloads),
            can_import=not issues,
            issues=issues,
            rows=previews,
        )
        return preview, payloads

    async def create_draft(self, source, business_date):
        self.opening.authorize()
        preview, lines = await self.validate(source, business_date)
        if not preview.can_import:
            raise ApiError(
                422, "opening_csv_errors", "Correct the CSV errors before creating its draft"
            )
        return await self.opening.save(OpeningIn(business_date=business_date, lines=lines))
