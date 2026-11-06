"""Pure catalog rules shared by editing, CSV validation and later stock services."""

import csv
import io
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from app.core.errors import ApiError

CSV_COLUMNS = (
    "sku",
    "name",
    "stock_unit",
    "item_type",
    "barcode",
    "purchase_unit",
    "purchase_factor",
    "sales_unit",
    "sales_factor",
    "cost_price",
    "selling_price",
    "gst_rate",
    "safety_stock",
    "returnable",
    "status",
    "valuation_method",
    "hsn_code",
)


def exact_stock_quantity(quantity: Decimal, factor: Decimal, decimal_places: int) -> Decimal:
    if not quantity.is_finite() or not factor.is_finite() or quantity <= 0 or factor <= 0:
        raise ApiError(422, "invalid_quantity", "Quantity and conversion factor must be positive")
    with localcontext() as context:
        context.prec = 80
        converted = quantity * factor
        try:
            rounded = converted.quantize(Decimal(1).scaleb(-decimal_places))
        except InvalidOperation:
            raise ApiError(
                422, "quantity_out_of_range", "The converted quantity is too large"
            ) from None
        if converted != rounded:
            raise ApiError(
                422, "fractional_quantity", "This quantity cannot be represented in the stock unit"
            )
        if converted >= Decimal("100000000000000"):
            raise ApiError(
                422, "quantity_out_of_range", "The converted quantity exceeds the supported range"
            )
        return converted


def assert_acyclic(kit_id: UUID, component_ids: list[UUID], edges: dict[UUID, set[UUID]]) -> None:
    graph = {key: set(values) for key, values in edges.items()}
    graph[kit_id] = set(component_ids)
    visiting: set[UUID] = set()
    visited: set[UUID] = set()
    # An iterative traversal avoids recursion failures for a deeply nested kit graph.
    stack = [(kit_id, False)]
    while stack:
        node, leaving = stack.pop()
        if leaving:
            visiting.remove(node)
            visited.add(node)
            continue
        if node in visiting:
            raise ApiError(
                422, "kit_cycle", "A virtual kit cannot contain itself, directly or indirectly"
            )
        if node in visited:
            continue
        visiting.add(node)
        stack.append((node, True))
        stack.extend((child, False) for child in graph.get(node, set()))


def parse_item_csv(source: str) -> list[dict[str, str]]:
    if "\x00" in source:
        raise ApiError(422, "invalid_csv", "The CSV contains an invalid null character")
    try:
        reader = csv.DictReader(io.StringIO(source.lstrip("\ufeff")), strict=True)
        headers = reader.fieldnames
        if not headers or len(headers) != len(set(headers)):
            raise ApiError(422, "invalid_csv", "Provide a CSV with distinct column headings")
        unknown = set(headers) - set(CSV_COLUMNS)
        if unknown or not {"sku", "name", "stock_unit", "item_type"}.issubset(headers):
            raise ApiError(
                422, "invalid_csv", "Use the catalog CSV template and its column headings"
            )
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ApiError(422, "invalid_csv", "Every CSV row must match the column headings")
            if not any(value.strip() for value in row.values()):
                continue
            rows.append({key: value.strip() for key, value in row.items()})
            if len(rows) > 500:
                raise ApiError(422, "csv_too_large", "Import at most 500 items at a time")
    except csv.Error:
        raise ApiError(
            422, "invalid_csv", "The CSV could not be read; check its quoting and columns"
        ) from None
    if not rows:
        raise ApiError(422, "empty_csv", "The CSV must contain at least one item")
    return rows


def csv_template() -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerow(
        {
            "sku": "SOAP",
            "name": "Soap bar",
            "stock_unit": "PCS",
            "item_type": "GOODS",
            "purchase_unit": "CTN",
            "purchase_factor": "48",
            "sales_unit": "PCS",
            "cost_price": "20.00",
            "selling_price": "25.00",
            "gst_rate": "18",
            "safety_stock": "0",
            "returnable": "true",
            "status": "ACTIVE",
            "valuation_method": "WEIGHTED_AVERAGE",
        }
    )
    return output.getvalue()
