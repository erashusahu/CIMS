"""Exercise the local catalog HTTP API, exclusively against its disposable test server."""

import uuid
from datetime import date
from decimal import Decimal

import httpx


def main() -> None:
    with httpx.Client(
        base_url="http://127.0.0.1:8001/api/v1/catalog",
        headers={"Origin": "http://127.0.0.1:5174"},
        timeout=15,
    ) as client:

        def call(method: str, path: str, body=None, status=200, headers=None):
            response = client.request(method, path, json=body, headers=headers)
            if response.status_code != status:
                raise AssertionError(
                    f"{method} {path}: expected {status}, got {response.status_code}"
                )
            return response.json()

        setup = call("GET", "/local-status")
        if not setup.get("isolated_test_database") or not setup["ready"]:
            raise RuntimeError(
                "Refusing test mutations: this is not the ready disposable database"
            )
        workspace = call(
            "POST",
            "/local-workspaces",
            {
                "name": "HTTP catalog acceptance",
                "slug": f"http-test-{uuid.uuid4().hex[:12]}",
            },
            status=201,
        )
        client.headers["X-CIMS-Tenant-ID"] = workspace["tenant_id"]
        pcs = call("POST", "/units", {"code": "PCS", "name": "Pieces"}, status=201)
        cartons = call("POST", "/units", {"code": "CTN", "name": "Cartons"}, status=201)
        classification = call(
            "POST", "/item-types", {"code": "GOODS", "name": "Goods"}, status=201
        )
        base = {
            "item_type_id": classification["item_type_id"],
            "stock_uom_id": pcs["uom_id"],
        }
        soap = call(
            "POST",
            "/items",
            {
                **base,
                "sku": "SOAP",
                "name": "Soap",
                "selling_price": "25.123456",
                "purchase_uom_id": cartons["uom_id"],
                "conversions": [{"from_uom_id": cartons["uom_id"], "factor": "48"}],
            },
            status=201,
        )
        assert soap["selling_price"] == "25.123456"
        result = call(
            "POST",
            "/convert",
            {
                "item_id": soap["item_id"],
                "from_uom_id": cartons["uom_id"],
                "quantity": "10",
            },
        )
        assert Decimal(result["stock_quantity"]) == Decimal(480)
        group = call(
            "POST",
            "/groups",
            {"code": "SOAP-SIZES", "name": "Soap sizes", "attributes": ["Size"]},
            status=201,
        )
        call(
            "POST",
            "/items",
            {
                **base,
                "sku": "SOAP-100",
                "name": "Soap 100g",
                "item_kind": "VARIANT",
                "item_group_id": group["item_group_id"],
                "attributes": {"Size": "100g"},
            },
            status=201,
        )
        call(
            "POST",
            "/items",
            {
                **base,
                "sku": "SOAP-KIT",
                "name": "Soap pair",
                "item_kind": "COMPOSITE",
                "components": [
                    {
                        "component_item_id": soap["item_id"],
                        "quantity": "2",
                        "uom_id": pcs["uom_id"],
                    }
                ],
            },
            status=201,
        )
        preview = call(
            "POST",
            "/imports/validate",
            {
                "csv_text": "sku,name,stock_unit,item_type\nCSV-SOAP,Imported soap,PCS,GOODS\n"
            },
        )
        assert preview["can_import"] and preview["valid_count"] == 1
        call(
            "POST",
            "/imports/commit",
            {
                "csv_text": "sku,name,stock_unit,item_type\nCSV-SOAP,Imported soap,PCS,GOODS\n"
            },
            status=201,
        )
        listing = call("GET", "/items")
        assert listing["total"] == 4
        print(
            "Catalog HTTP acceptance passed: workspace, units, item, "
            "conversion, variant, kit and CSV import."
        )
        inventory = "http://127.0.0.1:8001/api/v1/inventory"
        assert call("GET", inventory + "/opening-status")["ready"]
        location = call(
            "POST",
            inventory + "/locations",
            {
                "code": "MAIN",
                "name": "Main warehouse",
            },
            status=201,
        )
        document = call(
            "POST",
            inventory + "/opening-stock",
            {
                "business_date": date.today().isoformat(),
                "lines": [
                    {
                        "item_id": soap["item_id"],
                        "location_id": location["location_id"],
                        "uom_id": pcs["uom_id"],
                        "quantity": "10",
                        "unit_cost": "100",
                    }
                ],
            },
            status=201,
        )
        assert document["status"] == "DRAFT"
        assert call("GET", inventory + "/balances")["total"] == 0
        path = inventory + f"/opening-stock/{document['opening_stock_id']}/post"
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        posted = call(
            "POST", path, {"expected_version": document["version"]}, headers=headers
        )
        assert posted["status"] == "POSTED"
        assert posted == call(
            "POST", path, {"expected_version": document["version"]}, headers=headers
        )
        balance = call("GET", inventory + "/balances")["items"][0]
        assert Decimal(balance["physical_on_hand"]) == Decimal(10)
        assert Decimal(balance["accounting_on_hand"]) == Decimal(10)
        assert Decimal(balance["inventory_value"]) == Decimal(1000)
        assert call("GET", inventory + "/movements")["total"] == 2
        assert call("GET", inventory + "/reconciliation")["total"] == 0
        csv_body = {
            "business_date": date.today().isoformat(),
            "csv_text": "sku,location,unit,quantity,unit_cost\nSOAP,MAIN,CTN,10,100\n",
        }
        preview = call("POST", inventory + "/opening-stock/imports/validate", csv_body)
        assert preview["can_import"] and Decimal(
            preview["rows"][0]["stock_quantity"]
        ) == Decimal(480)
        imported = call(
            "POST",
            inventory + "/opening-stock/imports/create-draft",
            csv_body,
            status=201,
        )
        assert imported["status"] == "DRAFT"
        assert call("GET", inventory + "/movements")["total"] == 2
        print(
            "Inventory HTTP acceptance passed: draft, review, exactly-once post, "
            "balances, history, reconciliation and CSV draft."
        )


if __name__ == "__main__":
    main()
