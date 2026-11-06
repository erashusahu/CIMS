"""First procurement API slice: suppliers and preferred item suppliers."""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_access import CatalogContext, catalog_context
from app.core.errors import ApiError, count_freeze_error
from app.db.base import get_session
from app.models import catalog as catalog_db
from app.models import inventory as inventory_db
from app.models import procurement as procurement_db
from app.schemas.procurement import (
    PreferredSupplierIn,
    PreferredSupplierOut,
    PreferredSupplierPage,
    PurchaseOrderAction,
    PurchaseOrderAmendment,
    PurchaseOrderIn,
    PurchaseOrderOut,
    PurchaseOrderPage,
    PurchaseOrderUpdate,
    PurchaseSuggestionGroup,
    ReceiptIn,
    ReceiptOut,
    ReceiptPage,
    ReceiptPost,
    ReorderRuleIn,
    ReorderRuleOut,
    ReorderRulePage,
    SupplierCsvIn,
    SupplierImportPreview,
    SupplierIn,
    SupplierOut,
    SupplierPage,
    SupplierUpdate,
)
from app.services.procurement import ProcurementService, supplier_template
from app.services.purchase_orders import PurchaseOrderService
from app.services.purchase_pdf import render_purchase_order
from app.services.reorder import ReorderService

router = APIRouter(prefix="/procurement", tags=["procurement"])


async def procurement_service(
    context: CatalogContext = Depends(catalog_context), session: AsyncSession = Depends(get_session)
) -> ProcurementService:
    service = ProcurementService(session, context)
    await service.catalog.prepare()
    return service


async def commit_procurement(session, operation):
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
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise ApiError(
                409, "procurement_duplicate", "A supplier code or item link already exists"
            ) from None
        if sqlstate in {"23503", "23514", "23502"}:
            raise ApiError(
                422, "invalid_procurement_record", "Check the supplier and item details"
            ) from None
        raise
    except Exception:
        await session.rollback()
        raise


@router.get("/suppliers", response_model=SupplierPage)
async def suppliers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=100),
    service: ProcurementService = Depends(procurement_service),
):
    return await service.suppliers(page, page_size, search)


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_supplier(
    payload: SupplierIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(service.session, service.save_supplier(payload))


@router.patch("/suppliers/{vendor_id}", response_model=SupplierOut)
async def update_supplier(
    vendor_id: uuid.UUID,
    payload: SupplierUpdate,
    service: ProcurementService = Depends(procurement_service),
):
    return await commit_procurement(service.session, service.save_supplier(payload, vendor_id))


@router.get("/suppliers/imports/template", response_class=PlainTextResponse)
async def supplier_import_template(_: ProcurementService = Depends(procurement_service)):
    return PlainTextResponse(
        supplier_template(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=cims-suppliers-template.csv"},
    )


@router.post("/suppliers/imports/validate", response_model=SupplierImportPreview)
async def validate_supplier_import(
    payload: SupplierCsvIn, service: ProcurementService = Depends(procurement_service)
):
    return await service.validate_supplier_csv(payload.csv_text)


@router.post("/suppliers/imports/commit", response_model=SupplierImportPreview, status_code=201)
async def commit_supplier_import(
    payload: SupplierCsvIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(service.session, service.import_suppliers(payload.csv_text))


@router.get("/preferred-suppliers", response_model=PreferredSupplierPage)
async def preferred_suppliers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: ProcurementService = Depends(procurement_service),
):
    return await service.preferred_suppliers(page, page_size)


@router.post("/preferred-suppliers", response_model=PreferredSupplierOut, status_code=201)
async def set_preferred_supplier(
    payload: PreferredSupplierIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(service.session, service.set_preferred_supplier(payload))


@router.get("/reorder-rules", response_model=ReorderRulePage)
async def reorder_rules(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: ProcurementService = Depends(procurement_service),
):
    return await ReorderService(service.session, service.context).rules(page, page_size)


@router.post("/reorder-rules", response_model=ReorderRuleOut, status_code=201)
async def create_reorder_rule(
    payload: ReorderRuleIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(
        service.session, ReorderService(service.session, service.context).save_rule(payload)
    )


@router.put("/reorder-rules/{rule_id}", response_model=ReorderRuleOut)
async def update_reorder_rule(
    rule_id: uuid.UUID,
    payload: ReorderRuleIn,
    service: ProcurementService = Depends(procurement_service),
):
    return await commit_procurement(
        service.session,
        ReorderService(service.session, service.context).save_rule(payload, rule_id),
    )


@router.get("/suggestions", response_model=list[PurchaseSuggestionGroup])
async def purchase_suggestions(service: ProcurementService = Depends(procurement_service)):
    return await ReorderService(service.session, service.context).suggestions()


@router.get("/purchase-orders", response_model=PurchaseOrderPage)
async def purchase_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: ProcurementService = Depends(procurement_service),
):
    return await PurchaseOrderService(service.session, service.context).orders(page, page_size)


@router.get("/purchase-orders/{order_id}", response_model=PurchaseOrderOut)
async def purchase_order(
    order_id: uuid.UUID, service: ProcurementService = Depends(procurement_service)
):
    return await PurchaseOrderService(service.session, service.context).order(order_id)


@router.get("/purchase-orders/{order_id}/pdf")
async def purchase_order_pdf(
    order_id: uuid.UUID, service: ProcurementService = Depends(procurement_service)
):
    purchase = PurchaseOrderService(service.session, service.context)
    order = await purchase.order(order_id)
    if order["status"] in {"DRAFT", "PENDING_APPROVAL", "CANCELLED"}:
        raise ApiError(
            409, "purchase_pdf_unavailable", "Approve the purchase order before printing it"
        )
    vendor = await purchase.catalog.row(procurement_db.vendors, "vendor_id", order["vendor_id"])
    location = await purchase.catalog.row(
        inventory_db.locations, "location_id", order["location_id"]
    )
    items = {
        line["item_id"]: await purchase.catalog.row(catalog_db.items, "item_id", line["item_id"])
        for line in order["lines"]
    }
    content = render_purchase_order(order, vendor, location, items)
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={order['po_no'].replace('/', '-')}.pdf"
        },
    )


@router.post("/purchase-orders", response_model=PurchaseOrderOut, status_code=201)
async def create_purchase_order(
    payload: PurchaseOrderIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(
        service.session, PurchaseOrderService(service.session, service.context).save_order(payload)
    )


@router.put("/purchase-orders/{order_id}", response_model=PurchaseOrderOut)
async def update_purchase_order(
    order_id: uuid.UUID,
    payload: PurchaseOrderUpdate,
    service: ProcurementService = Depends(procurement_service),
):
    return await commit_procurement(
        service.session,
        PurchaseOrderService(service.session, service.context).save_order(payload, order_id),
    )


@router.post("/purchase-orders/{order_id}/actions/{action}", response_model=PurchaseOrderOut)
async def purchase_order_action(
    order_id: uuid.UUID,
    action: str,
    payload: PurchaseOrderAction,
    service: ProcurementService = Depends(procurement_service),
):
    if action not in {"submit", "approve", "send"}:
        raise ApiError(404, "purchase_action_not_found", "Unknown purchase order action")
    return await commit_procurement(
        service.session,
        PurchaseOrderService(service.session, service.context).action(
            order_id, action, payload.expected_updated_at
        ),
    )


@router.post("/purchase-orders/{order_id}/amendments", response_model=PurchaseOrderOut)
async def amend_purchase_order(
    order_id: uuid.UUID,
    payload: PurchaseOrderAmendment,
    service: ProcurementService = Depends(procurement_service),
):
    return await commit_procurement(
        service.session,
        PurchaseOrderService(service.session, service.context).amend(order_id, payload),
    )


@router.get("/receipts", response_model=ReceiptPage)
async def purchase_receipts(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    service: ProcurementService = Depends(procurement_service),
):
    return await PurchaseOrderService(service.session, service.context).receipts(page, page_size)


@router.get("/receipts/{receipt_id}", response_model=ReceiptOut)
async def purchase_receipt(
    receipt_id: uuid.UUID, service: ProcurementService = Depends(procurement_service)
):
    return await PurchaseOrderService(service.session, service.context).receipt(receipt_id)


@router.post("/receipts", response_model=ReceiptOut, status_code=201)
async def create_purchase_receipt(
    payload: ReceiptIn, service: ProcurementService = Depends(procurement_service)
):
    return await commit_procurement(
        service.session,
        PurchaseOrderService(service.session, service.context).save_receipt(payload),
    )


@router.post("/receipts/{receipt_id}/post", response_model=ReceiptOut)
async def post_purchase_receipt(
    receipt_id: uuid.UUID,
    payload: ReceiptPost,
    service: ProcurementService = Depends(procurement_service),
):
    return await commit_procurement(
        service.session,
        PurchaseOrderService(service.session, service.context).post_receipt(
            receipt_id, payload.idempotency_key
        ),
    )
