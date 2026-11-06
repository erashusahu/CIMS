"""Sales order drafts, availability, confirmation and cancellation."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.schemas.sales import (
    AvailabilityOut,
    CreditExposureOut,
    PickListOut,
    PickScanIn,
    PickScanOut,
    SalesAction,
    SalesCancel,
    SalesOrderIn,
    SalesOrderOut,
    SalesOrderPage,
    SalesOrderUpdate,
    ShipmentIn,
    ShipmentOut,
    ShipmentPost,
)
from app.services.sales_orders import SalesOrderService
from app.services.shipping import ShippingService
from app.services.shipping_pdf import packing_slip_pdf

router = APIRouter(prefix="/sales", tags=["sales"])


async def sales_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> SalesOrderService:
    service = SalesOrderService(session, context)
    await service.catalog.prepare()
    service.authorize_read()
    return service


async def shipping_service(
    context: CatalogContext = Depends(catalog_context),
    session: AsyncSession = Depends(get_session),
) -> ShippingService:
    service = ShippingService(session, context)
    await service.catalog.prepare()
    service.sales.authorize_read()
    return service


@router.get("/shipping-status")
async def shipping_status(service: ShippingService = Depends(shipping_service)):
    return {
        "ready": bool(
            await service.session.scalar(text("SELECT to_regclass('cims.pick_scan') IS NOT NULL"))
        )
    }


async def commit_sales(session: AsyncSession, operation):
    try:
        result = await operation
        await session.commit()
        return result
    except IntegrityError as exc:
        await session.rollback()
        if count_freeze_error(exc):
            raise ApiError(
                409, "count_scope_frozen", "Stock is frozen for an active count"
            ) from None
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise ApiError(
                409, "sales_conflict", "This sales order conflicts with an existing record"
            ) from None
        if code in {"23503", "23514", "23502"}:
            raise ApiError(422, "invalid_sales_order", "Review the sales order details") from None
        raise
    except Exception:
        await session.rollback()
        raise


@router.get("/availability", response_model=AvailabilityOut)
async def availability(
    item_id: uuid.UUID,
    location_id: uuid.UUID,
    service: SalesOrderService = Depends(sales_service),
):
    return await service.availability(item_id, location_id)


@router.get("/customers/{customer_id}/credit", response_model=CreditExposureOut)
async def credit_exposure(
    customer_id: uuid.UUID, service: SalesOrderService = Depends(sales_service)
):
    return await service.credit_exposure(customer_id)


@router.get("/orders", response_model=SalesOrderPage)
async def orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    source: Literal["MANUAL_RETAIL", "WHOLESALE", "POS"] | None = None,
    service: SalesOrderService = Depends(sales_service),
):
    return await service.orders(page, page_size, source)


@router.post("/orders", response_model=SalesOrderOut, status_code=201)
async def create_order(payload: SalesOrderIn, service: SalesOrderService = Depends(sales_service)):
    return await commit_sales(service.session, service.save(payload))


@router.get("/orders/{order_id}", response_model=SalesOrderOut)
async def order(order_id: uuid.UUID, service: SalesOrderService = Depends(sales_service)):
    return await service.order(order_id)


@router.patch("/orders/{order_id}", response_model=SalesOrderOut)
async def update_order(
    order_id: uuid.UUID,
    payload: SalesOrderUpdate,
    service: SalesOrderService = Depends(sales_service),
):
    return await commit_sales(service.session, service.save(payload, order_id))


@router.post("/orders/{order_id}/confirm", response_model=SalesOrderOut)
async def confirm_order(
    order_id: uuid.UUID,
    payload: SalesAction,
    service: SalesOrderService = Depends(sales_service),
):
    return await commit_sales(
        service.session,
        service.confirm(order_id, payload.expected_revision, payload.idempotency_key),
    )


@router.post("/orders/{order_id}/cancel", response_model=SalesOrderOut)
async def cancel_order(
    order_id: uuid.UUID,
    payload: SalesCancel,
    service: SalesOrderService = Depends(sales_service),
):
    return await commit_sales(
        service.session,
        service.cancel(
            order_id, payload.expected_revision, payload.idempotency_key, payload.reason
        ),
    )


@router.get("/orders/{order_id}/pick-list", response_model=PickListOut)
async def pick_list(order_id: uuid.UUID, service: ShippingService = Depends(shipping_service)):
    return await service.pick_list(order_id)


@router.get("/orders/{order_id}/scans", response_model=list[PickScanOut])
async def pick_scans(order_id: uuid.UUID, service: ShippingService = Depends(shipping_service)):
    return await service.scans(order_id)


@router.post("/orders/{order_id}/scans", response_model=PickScanOut, status_code=201)
async def scan_item(
    order_id: uuid.UUID, payload: PickScanIn, service: ShippingService = Depends(shipping_service)
):
    return await commit_sales(service.session, service.scan(order_id, payload))


@router.get("/orders/{order_id}/shipments", response_model=list[ShipmentOut])
async def order_shipments(
    order_id: uuid.UUID, service: ShippingService = Depends(shipping_service)
):
    return await service.shipments(order_id)


@router.post("/orders/{order_id}/shipments", response_model=ShipmentOut, status_code=201)
async def pack_shipment(
    order_id: uuid.UUID, payload: ShipmentIn, service: ShippingService = Depends(shipping_service)
):
    return await commit_sales(service.session, service.draft(order_id, payload))


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut)
async def shipment(shipment_id: uuid.UUID, service: ShippingService = Depends(shipping_service)):
    return await service.shipment(shipment_id)


@router.post("/shipments/{shipment_id}/post", response_model=ShipmentOut)
async def post_shipment(
    shipment_id: uuid.UUID,
    payload: ShipmentPost,
    service: ShippingService = Depends(shipping_service),
):
    return await commit_sales(service.session, service.post(shipment_id, payload.idempotency_key))


@router.post("/shipments/{shipment_id}/cancel", response_model=ShipmentOut)
async def cancel_shipment_draft(
    shipment_id: uuid.UUID, service: ShippingService = Depends(shipping_service)
):
    return await commit_sales(service.session, service.cancel_draft(shipment_id))


@router.get("/shipments/{shipment_id}/packing-slip")
async def packing_slip(
    shipment_id: uuid.UUID, service: ShippingService = Depends(shipping_service)
):
    document = await service.shipment(shipment_id)
    order = await service.sales.order(document["sales_order_id"])
    content = await packing_slip_pdf(service, document, order)
    filename = document["shipment_no"].replace("/", "-") + "-packing-slip.pdf"
    return Response(
        content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
