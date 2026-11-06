"""Exactly-once sync of a small, explicit set of mobile draft operations."""

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import insert, select, text

from app.core.errors import ApiError
from app.models import mobile as db
from app.schemas.procurement import ReceiptIn, ReceiptOut
from app.schemas.recovery import CountOut, CountSubmit
from app.schemas.sales import (
    PickScanIn,
    PickScanOut,
    SalesOrderIn,
    SalesOrderOut,
    ShipmentIn,
    ShipmentOut,
)
from app.schemas.transfers import TransferOut, TransferQuantityAction
from app.services.counts import CountService
from app.services.purchase_orders import PurchaseOrderService
from app.services.sales_orders import SalesOrderService
from app.services.shipping import ShippingService
from app.services.transfers import TransferService


class MobileTransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: uuid.UUID
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class MobileService:
    def __init__(self, session, context):
        self.session, self.context = session, context

    @property
    def actor_key(self):
        return "local" if self.context.local else str(self.context.user_id)

    def scope(self):
        return db.receipts.c.tenant_id == self.context.tenant_id

    @staticmethod
    def version_matches(actual, expected):
        try:
            return actual == datetime.fromisoformat(expected.replace("Z", "+00:00"))
        except ValueError:
            return False

    @staticmethod
    def output(row):
        return {
            "operation_id": row["operation_id"],
            "kind": row["operation_kind"],
            "document_id": row["document_id"],
            "captured_at": row["captured_at"],
            "accepted_at": row["accepted_at"],
            "result": row["result"],
        }

    async def receipt(self, operation_id):
        row = (
            (
                await self.session.execute(
                    select(db.receipts).where(
                        self.scope(),
                        db.receipts.c.operation_id == operation_id,
                        db.receipts.c.actor_key == self.actor_key,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(404, "mobile_receipt_missing", "No accepted operation has this ID")
        return self.output(row)

    async def submit(self, operation):
        if operation.operation_id in operation.depends_on or len(set(operation.depends_on)) != len(
            operation.depends_on
        ):
            raise ApiError(422, "mobile_dependencies_invalid", "Review operation dependencies")
        fingerprint = hashlib.sha256(
            json.dumps(
                operation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cims.mobile:{self.context.tenant_id}:{operation.operation_id}"},
        )
        existing = (
            (
                await self.session.execute(
                    select(db.receipts).where(
                        self.scope(), db.receipts.c.operation_id == operation.operation_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["actor_key"] != self.actor_key or existing["request_hash"] != fingerprint:
                raise ApiError(
                    409, "mobile_operation_conflict", "This operation ID belongs to different input"
                )
            return self.output(existing)
        for dependency in operation.depends_on:
            accepted = await self.session.scalar(
                select(db.receipts.c.operation_id).where(
                    self.scope(),
                    db.receipts.c.operation_id == dependency,
                    db.receipts.c.actor_key == self.actor_key,
                )
            )
            if accepted is None:
                raise ApiError(
                    409, "mobile_predecessor_pending", "Synchronize earlier actions first"
                )
        try:
            result, response_type = await self.dispatch(operation)
        except ValidationError:
            raise ApiError(
                422, "mobile_payload_invalid", "Review mobile operation fields"
            ) from None
        serial = response_type.model_validate(result).model_dump(mode="json")
        row = (
            (
                await self.session.execute(
                    insert(db.receipts)
                    .values(
                        operation_id=operation.operation_id,
                        tenant_id=self.context.tenant_id,
                        actor_key=self.actor_key,
                        operation_kind=operation.kind,
                        document_id=operation.document_id,
                        request_hash=fingerprint,
                        captured_at=operation.captured_at,
                        result=serial,
                    )
                    .returning(db.receipts)
                )
            )
            .mappings()
            .one()
        )
        return self.output(row)

    async def dispatch(self, operation):
        kind, document_id = operation.kind, operation.document_id
        if kind != "POS_DRAFT" and document_id is None:
            raise ApiError(422, "mobile_document_required", "Choose a document")
        if kind != "POS_DRAFT" and not operation.expected_version:
            raise ApiError(422, "mobile_version_required", "Refresh this document before capture")
        if kind == "RECEIPT_DRAFT":
            data = ReceiptIn.model_validate(operation.payload)
            if data.purchase_order_id != document_id:
                raise ApiError(
                    422, "mobile_document_mismatch", "Choose the matching purchase order"
                )
            service = PurchaseOrderService(self.session, self.context)
            if operation.expected_version:
                order = await service.order(document_id, lock=True)
                if not self.version_matches(order["updated_at"], operation.expected_version):
                    raise ApiError(409, "mobile_document_stale", "Reload the purchase order")
            return await service.save_receipt(data), ReceiptOut
        if kind in {"PICK_SCAN", "SHIPMENT_PACK"}:
            service = ShippingService(self.session, self.context)
            if operation.expected_version:
                order = await service.sales.order(document_id, lock=True)
                if str(order["revision"]) != operation.expected_version:
                    raise ApiError(409, "mobile_document_stale", "Reload the sales order")
            if kind == "PICK_SCAN":
                return await service.scan(
                    document_id, PickScanIn.model_validate(operation.payload)
                ), PickScanOut
            return await service.draft(
                document_id, ShipmentIn.model_validate(operation.payload)
            ), ShipmentOut
        if kind == "TRANSFER_RECEIVE":
            service = TransferService(self.session, self.context)
            if operation.expected_version:
                document = await service.document(document_id, lock=True)
                if str(len(document["operations"])) != operation.expected_version:
                    raise ApiError(409, "mobile_document_stale", "Reload the transfer")
            data = MobileTransferIn.model_validate(operation.payload)
            action = TransferQuantityAction(
                line_id=data.line_id,
                quantity=data.quantity,
                idempotency_key=f"mobile-{operation.operation_id.hex}",
            )
            return await service.resolve(document_id, action), TransferOut
        if kind == "COUNT_SUBMIT":
            service = CountService(self.session, self.context)
            if operation.expected_version:
                document = await service.document(document_id, lock=True)
                if not self.version_matches(document["snapshot_at"], operation.expected_version):
                    raise ApiError(409, "mobile_document_stale", "Reload the count")
            return await service.submit(
                document_id, CountSubmit.model_validate(operation.payload)
            ), CountOut
        if kind == "POS_DRAFT":
            if document_id is not None:
                raise ApiError(
                    422, "mobile_document_mismatch", "A new POS draft has no document ID"
                )
            data = SalesOrderIn.model_validate(operation.payload)
            if data.sales_source != "POS":
                raise ApiError(422, "mobile_pos_draft_only", "Only POS drafts can be saved here")
            return await SalesOrderService(self.session, self.context).save(data), SalesOrderOut
        raise ApiError(422, "mobile_kind_invalid", "Choose a supported mobile action")
