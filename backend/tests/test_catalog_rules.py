import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.api.catalog_access import CatalogContext
from app.api.v1.catalog import commit_operation
from app.core.errors import ApiError
from app.models import catalog as db
from app.schemas.catalog import GroupIn, GroupOut, ItemIn, ItemUpdate
from app.services.catalog import CatalogService
from app.services.catalog_rules import (
    assert_acyclic,
    csv_template,
    exact_stock_quantity,
    parse_item_csv,
)
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError


def item_values(**changes):
    return {
        "sku": "SOAP",
        "name": "Soap",
        "item_type_id": uuid.uuid4(),
        "stock_uom_id": uuid.uuid4(),
        **changes,
    }


def service(role="Owner"):
    context = CatalogContext(uuid.uuid4(), uuid.uuid4(), role, "catalog-test")
    return CatalogService(AsyncMock(), context)


def test_ten_cartons_of_48_are_480_pieces():
    assert exact_stock_quantity(Decimal("10"), Decimal("48"), 0) == Decimal("480")


@pytest.mark.parametrize(
    "quantity,factor,precision,expected",
    [
        ("1.25", "1000", 0, "1250"),
        ("3", "0.125", 3, "0.375"),
        ("0.1", "0.2", 2, "0.02"),
        ("99999999999999", "1", 0, "99999999999999"),
    ],
)
def test_decimal_conversion_is_exact(quantity, factor, precision, expected):
    assert exact_stock_quantity(Decimal(quantity), Decimal(factor), precision) == Decimal(expected)


@pytest.mark.parametrize(
    "quantity,factor,precision",
    [
        ("1", "0.5", 0),
        ("1.1", "1", 0),
        ("1", "0.00001", 4),
    ],
)
def test_unsupported_fraction_is_rejected_instead_of_rounded(quantity, factor, precision):
    with pytest.raises(ApiError) as failure:
        exact_stock_quantity(Decimal(quantity), Decimal(factor), precision)
    assert failure.value.code == "fractional_quantity"


@pytest.mark.parametrize(
    "quantity,factor", [("NaN", "1"), ("Infinity", "1"), ("0", "1"), ("1", "-1")]
)
def test_invalid_conversion_values_are_rejected(quantity, factor):
    with pytest.raises(ApiError):
        exact_stock_quantity(Decimal(quantity), Decimal(factor), 0)


def test_quantity_overflow_is_rejected():
    with pytest.raises(ApiError) as failure:
        exact_stock_quantity(Decimal("100000000000000"), Decimal(1), 0)
    assert failure.value.code == "quantity_out_of_range"


def test_kit_rejects_self_reference_and_indirect_cycles():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for children, edges in [([a], {}), ([b], {b: {c}, c: {a}})]:
        with pytest.raises(ApiError) as failure:
            assert_acyclic(a, children, edges)
        assert failure.value.code == "kit_cycle"


def test_shared_components_are_allowed_and_source_graph_is_not_modified():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    edges = {b: {d}, c: {d}}
    assert_acyclic(a, [b, c], edges)
    assert edges == {b: {d}, c: {d}}


def test_deep_kit_validation_does_not_recurse_on_python_stack():
    nodes = [uuid.uuid4() for _ in range(2000)]
    assert_acyclic(
        nodes[0], [nodes[1]], {nodes[index]: {nodes[index + 1]} for index in range(1, 1999)}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"selling_price": "-1"},
        {"gst_rate": "101"},
        {"safety_stock": "0.00001"},
        {"cost_price": "0.0000001"},
        {"selling_price": "NaN"},
        {"is_tax_exempt": True, "gst_rate": "18"},
        {"item_kind": "VARIANT"},
        {"item_group_id": uuid.uuid4()},
        {"item_kind": "COMPOSITE"},
        {"tenant_id": uuid.uuid4()},
        {"stock_quantity": "100"},
    ],
)
def test_item_rejects_invalid_values_and_client_supplied_tenant_or_stock(changes):
    with pytest.raises(ValidationError):
        ItemIn(**item_values(**changes))


def test_component_and_conversion_duplicates_are_rejected():
    component, unit = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ValidationError):
        ItemIn(
            **item_values(
                item_kind="COMPOSITE",
                components=[
                    {"component_item_id": component, "uom_id": unit, "quantity": "1"},
                    {"component_item_id": component, "uom_id": unit, "quantity": "2"},
                ],
            )
        )
    with pytest.raises(ValidationError):
        ItemIn(**item_values(conversions=[{"from_uom_id": unit, "factor": "10"}] * 2))


def test_variant_group_requires_distinct_attributes():
    for attributes in [[], ["Size", "size"], [" "]]:
        with pytest.raises(ValidationError):
            GroupIn(code="SOAP", name="Soap", attributes=attributes)


def test_group_response_omits_internal_database_fields():
    response = GroupOut.model_validate({
        "tenant_id": uuid.uuid4(), "item_group_id": uuid.uuid4(), "code": "SOAP",
        "name": "Soap", "description": None, "valuation_method": None,
        "default_item_type_id": None, "is_active": True, "attributes": ["Size"],
    })
    assert "tenant_id" not in response.model_dump()


def test_template_round_trip_and_utf8_bom():
    rows = parse_item_csv("\ufeff" + csv_template())
    assert rows[0]["sku"] == "SOAP"
    assert rows[0]["purchase_factor"] == "48"


@pytest.mark.parametrize(
    "source",
    [
        "sku,sku,name,stock_unit,item_type\nA,A,A,PCS,GOODS",
        "sku,name\nA,A",
        "sku,name,stock_unit,item_type,unknown\nA,A,PCS,GOODS,no",
        'sku,name,stock_unit,item_type\nA,"unfinished',
        "sku,name,stock_unit,item_type\nA,Soap,PCS,GOODS,extra",
        "sku,name,stock_unit,item_type\nA,Soap,PCS",
        "sku,name,stock_unit,item_type\n",
        "sku,name,stock_unit,item_type\nA,So\x00ap,PCS,GOODS",
    ],
)
def test_malformed_csv_is_rejected(source):
    with pytest.raises(ApiError):
        parse_item_csv(source)


def test_csv_row_limit_is_enforced():
    with pytest.raises(ApiError) as failure:
        parse_item_csv("sku,name,stock_unit,item_type\n" + "A,Soap,PCS,GOODS\n" * 501)
    assert failure.value.code == "csv_too_large"


@pytest.mark.asyncio
async def test_viewer_cannot_write_even_if_calling_service_directly():
    catalog = service("Viewer")
    with pytest.raises(ApiError) as failure:
        await catalog.lock()
    assert failure.value.status_code == 403
    catalog.session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_row_lookup_scopes_identifiers_to_the_selected_business():
    catalog = service()
    result = MagicMock()
    result.mappings.return_value.first.return_value = None
    catalog.session.execute.return_value = result
    target_id = uuid.uuid4()
    with pytest.raises(ApiError) as failure:
        await catalog.row(db.items, "item_id", target_id)
    assert failure.value.status_code == 404
    statement = catalog.session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert catalog.tenant_id in compiled.params.values()
    assert target_id in compiled.params.values()
    assert "cims.item.tenant_id =" in str(compiled)


@pytest.mark.asyncio
async def test_stale_item_edit_is_rejected_before_any_record_changes():
    catalog = service()
    catalog.lock = AsyncMock()
    catalog.row = AsyncMock(return_value={"catalog_version": 4})
    payload = ItemUpdate(**item_values(), expected_version=3)
    with pytest.raises(ApiError) as failure:
        await catalog.save_item(payload, uuid.uuid4())
    assert failure.value.code == "stale_catalog_item"
    catalog.session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_csv_dry_run_reports_case_insensitive_duplicates_without_writes():
    catalog = service()
    pcs, goods = uuid.uuid4(), uuid.uuid4()
    catalog.options = AsyncMock(
        return_value={
            "units": [{"code": "PCS", "uom_id": pcs}],
            "item_types": [{"code": "GOODS", "item_type_id": goods}],
            "hsn_codes": [],
        }
    )
    catalog.validate_item = AsyncMock()
    result = MagicMock()
    result.all.return_value = [("EXISTING", "123")]
    catalog.session.execute.return_value = result
    preview, valid = await catalog.validate_csv(
        "sku,name,stock_unit,item_type,barcode\n"
        "SOAP,Soap,PCS,GOODS,999\nsoap,Soap 2,PCS,GOODS,999\nexisting,Other,PCS,GOODS,123\n"
    )
    assert not preview.can_import
    assert preview.valid_count == len(valid) == 1
    assert {(issue.row, issue.field) for issue in preview.issues} == {
        (3, "sku"),
        (3, "barcode"),
        (4, "sku"),
        (4, "barcode"),
    }
    assert all(call.args[0].is_select for call in catalog.session.execute.await_args_list)
    catalog.session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_write_rolls_back_instead_of_committing_partial_results():
    session = AsyncMock()

    async def failing():
        raise ApiError(422, "kit_cycle", "Invalid kit")

    with pytest.raises(ApiError):
        await commit_operation(session, failing())
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unique_constraint_failure_is_a_safe_conflict():
    session = AsyncMock()
    original = Exception("private SQL parameters")
    original.sqlstate = "23505"

    async def duplicate():
        raise IntegrityError("private SQL", {}, original)

    with pytest.raises(ApiError) as failure:
        await commit_operation(session, duplicate())
    assert failure.value.status_code == 409
    assert "private" not in failure.value.message
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_commit_revalidates_before_any_inserts():
    catalog = service()
    catalog.lock = AsyncMock()
    from app.schemas.catalog import ImportPreview

    catalog.validate_csv = AsyncMock(
        return_value=(
            ImportPreview(row_count=1, valid_count=0, issues=[], can_import=False, rows=[]),
            [],
        )
    )
    catalog.save_item = AsyncMock()
    with pytest.raises(ApiError) as failure:
        await catalog.commit_csv("changed file")
    assert failure.value.code == "csv_validation_failed"
    catalog.validate_csv.assert_awaited_once_with("changed file")
    catalog.save_item.assert_not_awaited()
