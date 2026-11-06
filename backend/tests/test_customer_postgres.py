"""First Phase 3C customer-setup checks on isolated PostgreSQL."""

import os

import pytest

from app.api.v1.procurement import commit_procurement
from app.core.errors import ApiError
from app.schemas.customer import AddressIn, CustomerIn, CustomerUpdate
from app.services.customer import CustomerService

pytestmark = pytest.mark.skipif(
    not os.getenv("CIMS_CATALOG_TEST_URL"), reason="Isolated PostgreSQL fixture required"
)


@pytest.mark.asyncio
async def test_customer_terms_credit_address_and_tenant_scope(catalog_db):
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        customer = await commit_procurement(
            catalog.session,
            service.save(
                CustomerIn(
                    code="BUYER-01",
                    name="Buyer One",
                    payment_terms_days=30,
                    credit_limit="50000",
                    place_of_supply_state="27",
                )
            ),
        )
    assert customer["credit_limit"] == 50000
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        address = await commit_procurement(
            catalog.session,
            service.add_address(
                customer["customer_id"],
                AddressIn(
                    address_type="BILLING",
                    line1="15 Market Road",
                    city="Pune",
                    state_code="27",
                    is_default=True,
                ),
            ),
        )
    assert address["customer_id"] == customer["customer_id"]
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        assert len(await service.addresses(customer["customer_id"])) == 1
        assert (await service.customers(1, 10, "Buyer"))["total"] == 1
    async with catalog_db["catalog"](catalog_db["tenants"][1]) as catalog:
        service = CustomerService(catalog.session, catalog.context)
        assert (await service.customers(1, 10))["total"] == 0
        with pytest.raises(ApiError):
            await service.addresses(customer["customer_id"])


@pytest.mark.asyncio
async def test_customer_duplicate_and_stale_edit(catalog_db):
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        customer = await commit_procurement(
            catalog.session, service.save(CustomerIn(code="BUYER-02", name="Buyer Two"))
        )
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as duplicate:
            await commit_procurement(
                catalog.session, service.save(CustomerIn(code="buyer-02", name="Other"))
            )
        assert duplicate.value.code == "customer_code_exists"
    update = CustomerUpdate(
        code="BUYER-02", name="Buyer Revised", expected_updated_at=customer["updated_at"]
    )
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        revised = await commit_procurement(
            catalog.session, service.save(update, customer["customer_id"])
        )
        assert revised["name"] == "Buyer Revised"
    async with catalog_db["catalog"]() as catalog:
        service = CustomerService(catalog.session, catalog.context)
        with pytest.raises(ApiError) as stale:
            await commit_procurement(catalog.session, service.save(update, customer["customer_id"]))
        assert stale.value.code == "stale_customer"
