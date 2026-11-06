# CIMS implementation status

Updated: 2026-11-06.

## Scope and decisions

The three supplied plans were inspected: PLAN (26) covers Chunk 1, PLAN (27) covers Chunk 2, and PLAN (28) replaces the former Chunks 3–12 with stages 3A–3J.

The user explicitly deferred authentication and requested Chunk 2 followed by Chunk 3. The implementation therefore provides an explicit loopback-only local workspace mode. This is not completion or acceptance of the authenticated foundation.

The user clarified that billing and invoices are developed separately. Their document creation, posting, PDFs and related workflows are outside CIMS. This overrides the billing/invoice work listed under Stage 3F in PLAN (28). Any future connection for accounting quantities, credit exposure, payments or Tally export needs an agreed interface with that separate system.

The user retains control of database extensions. Existing `localhost:5432/Inventroy` was inspected read-only. Schema changes and acceptance-test records were confined to a disposable PostgreSQL cluster on port 55439.

## Delivery status

| Milestone | Status |
|---|---|
| Chunk 1 authentication, staff, role/location access | Deferred. Existing public-schema scaffold does not match the live cims foundation. |
| Chunk 2 catalog | Implemented and verified with unit, component, native PostgreSQL, and HTTP checks. SQL 002 is confirmed installed. |
| Chunk 3A inventory/opening stock | Implemented for local Owner/Admin work, with native PostgreSQL and HTTP checks. Live use additionally requires SQL 003. |
| Chunk 3B procurement/receiving | Implemented and verified on isolated PostgreSQL. Live use requires SQL 003 and 004, applied by the user. |
| Chunk 3C sales orders/reservations | Implemented and verified on isolated PostgreSQL. Live use requires SQL 003, 004 and 005, applied by the user. |
| Chunk 3D picking/packing/shipping | Implemented and verified on isolated PostgreSQL. Live use requires SQL 006 after 003–005, applied by the user. |
| Chunk 3E location transfers | Implemented and verified on isolated PostgreSQL. Live use requires SQL 007 after 003–006, applied by the user. |
| Chunk 3F | Billing and invoicing excluded; any external-system integration is undefined. |
| Stage 3G physical returns and counts | Implemented and verified on isolated PostgreSQL. Live use requires SQL 008 after 003–007, applied by the user. Credit notes, refunds and accounting value adjustments remain external. |
| Stage 3H POS | Draft carts, sales-source tracking, barcode/SKU search and walk-in customer setup implemented and verified on isolated PostgreSQL. Live use requires SQL 009 after 003–008. Checkout, shifts, receipts and cash reconciliation await the external billing/payment interface. |
| Stage 3I mobile and offline drafts | Implemented for the local workspace and verified with isolated PostgreSQL and IndexedDB tests. Live synchronization requires SQL 010 after 003–009. Authenticated offline reload and device access control require the deferred identity foundation. |
| Stage 3J reports and pilot readiness | Operational overview, stock/valuation and movement views, FIFO aging, sales and purchasing order summaries, and order-tax estimates implemented. Guarded backup/restore rehearsal, ledger check, runbook and 50-concurrent stock-query harness added. Representative-data performance, Hindi support and security completion remain. Financial settlement depends on the separate billing system. |

## Chunk 2 evidence

- `backend/app/models/catalog.py` maps the existing cims tables without runtime schema creation.
- `backend/app/services/catalog.py` owns tenant-filtered setup, item edits, variants, kit composition, and atomic catalog imports.
- `backend/app/services/catalog_rules.py` checks exact decimal conversions and prevents cyclic kits.
- `backend/app/api/catalog_access.py` restricts the explicit unauthenticated mode to development, loopback client/Host, and the configured origin.
- `frontend/src/features/catalog/pages.tsx` supplies setup, editor, conversion preview, listing and CSV workflows.
- Catalog changes never manufacture stock quantities.
- Native checks cover case-insensitive identifiers within a tenant, RLS with a non-owner/NOBYPASSRLS role, cross-tenant references, concurrency, version conflicts, conversion precision, and import rollback.

## Chunk 3A evidence

- `InventoryPostingService` in `backend/app/services/opening_stock.py` is the application owner of ledger INSERTs. Physical and accounting receipt entries share one transaction.
- Existing database posting triggers update balances. No second application quantity update occurs.
- Opening documents have number sequences, saved quantity/unit/cost snapshots, explicit posting, expected versions, stable idempotency receipts, and database guards on posted headers/lines.
- All posting effects, audits, FIFO layers, valuation lock, receipt and outbox event commit or roll back together.
- Stock unit/valuation become locked at first posting. Effective item/group/tenant valuation is stored on the item.
- `OpeningImporter` validates CSV and creates drafts. Stock is posted only by the explicit posting action.
- `InventoryService` provides physical/accounting/committed/incoming balances, available/ATP quantities, history and reconciliation. FIFO valuation sums actual remaining receipt layers and flags incomplete layers.
- `OutboxWorker` claims committed tenant jobs with FOR UPDATE SKIP LOCKED. Reconciliation failures receive safe error codes and delayed retries.
- UI source: `frontend/src/features/inventory`. API contract: generated `frontend/src/api/openapi.json` and `openapi.d.ts`.

## Chunk 3B evidence

- The existing `cims.vendor` and `cims.item_vendor` tables already cover supplier setup and preferred sources; this increment needs no new table.
- `backend/app/services/procurement.py` implements tenant-scoped supplier create/edit/list, exact case-insensitive code checks, CSV dry-run and atomic import, and one preferred supplier per stockable item.
- `backend/app/api/v1/procurement.py` exposes those operations under `/api/v1/procurement`; generated API types and `frontend/src/features/procurement/pages.tsx` add supplier, preferred-source and CSV screens.
- The shared UI uses Ice Silver, Deep Aubergine and Marigold in `frontend/src/styles.css`, including responsive layout, focus states and a visible mobile sign-out action.
- `ReorderService` saves per-item/location rules and calculates live suggestions grouped by supplier. Suggestions include outstanding sent orders and safety stock.
- `PurchaseOrderService` creates and revises drafts, applies INR 50,000 value-based approval, records manual sending, and requires an approved amendment before excess can be received.
- Receipt drafts record accepted, short and damaged units. Posting writes physical ledger entries through `InventoryPostingService`; accounting quantity remains unchanged. Idempotency receipts and row locks prevent duplicate stock.
- Damaged units post to a dedicated quarantine location. The stock read side marks that location ineligible for sales. FIFO layers, valuation locking, audit records and a reconciliation outbox event share the posting transaction.
- The API and purchasing UI cover rules, grouped suggestions, order creation, approval, amendments, receiving and PDF download. A suggestion can prefill a draft order for review.
- SQL 004 guards posted receipt lines at database level. No base procurement table was missing.

## Chunk 3C evidence

- Existing `cims.customer`, `cims.party_address`, `cims.sales_order`, `cims.sales_order_line`, and `cims.reservation` tables cover the base records. No additional base table is needed. SQL 005 adds guards for confirmed order details and reservation snapshots.
- `CustomerService` supports customer terms, credit limits, addresses, CSV preview and atomic import. The Customers screen offers a file upload and validation before import.
- `SalesOrderService` prices drafts on the backend, calculates estimated tax, checks per-location stock and credit exposure, and confirms all line reservations in one transaction. Confirm and cancel use idempotency keys and revision checks.
- Reservations store component item, location and quantity at confirmation. Subsequent virtual-kit edits do not change an existing order's reservation snapshot.
- Reservation holds and releases are committed-ledger entries. Existing PostgreSQL triggers update `stock_balance.committed`; the app does not write that balance directly. Cancellation releases only the unconsumed portion of each reservation.
- The Sales orders screen lists and edits drafts, shows availability and credit, confirms orders, and cancels unshipped quantities. Generated OpenAPI contracts cover the new routes.

## Chunk 3D evidence

- Existing `cims.package`, `cims.package_line`, `cims.shipment`, and `cims.shipment_line` hold the packing and shipment documents. SQL 006 adds the missing `cims.pick_scan` table for scan and override evidence, with tenant RLS, audit and immutability guards. It also protects shipped package and shipment details.
- `ShippingService` exposes per-order pick lists, barcode/SKU checks, exact quantity limits and reasoned manager overrides. A packed shipment can contain multiple packages, dimensions, weight, component contents and optional carrier/tracking details.
- Picking and packing leave physical and committed stock unchanged. Posting a shipment consumes the component reservation and posts a physical issue through `InventoryPostingService` in one transaction. FIFO issues consume layers and record their provenance.
- Partial shipments keep the remaining reservation active. Idempotency keys make repeated posting safe; a failed later line rolls back earlier line movements. The sales-order cancellation path blocks while a packed draft is open and preserves already shipped quantities.
- The Sales orders screen supports scan entry from a keyboard/scanner, package creation, packing-slip download and explicit ship posting. The API returns a packing-slip PDF.

## Chunk 3E evidence

- Existing `cims.transfer_order` and `cims.transfer_order_line` store transfer documents. SQL 007 adds immutable `cims.transfer_operation` rows, tenant RLS, audit, and guards for confirmed transfer details and status transitions.
- The Transfers screen and `/api/v1/transfers` routes provide GST registration mapping, drafts, confirmation, dispatch, partial receipts, in-transit history and reasoned loss settlement. Different or unmapped GST registrations are blocked.
- Confirmation holds source `COMMITTED` stock and destination `INCOMING` stock. Dispatch releases the source hold and moves physical stock into a dedicated in-transit system location. Receipt reduces in-transit and incoming quantities and adds physical destination stock. These ledger effects commit together with document progress, FIFO layers, provenance, audit and outbox events.
- A 40-unit dispatch from 100 leaves 60 at source and 40 in transit. Receiving 38 leaves two in transit. An Owner/Admin can record a reasoned loss settlement for the remaining two. Ordinary movements preserve total physical inventory value in the tested weighted-average and FIFO paths. Idempotency keys prevent retry duplication.
- The API blocks batch, lot, serial and expiry-tracked items until transfer-specific tracking details are implemented. The local workspace records a reason for loss settlement but has no named approver while authentication remains deferred.

## Stage 3G physical operations evidence

- Existing `cims.sales_return`, `cims.sales_return_line`, `cims.audit_count`, `cims.audit_count_line`, `cims.adjustment` and `cims.reason_code` tables hold return and count records. SQL 008 adds database guards and a ledger trigger for count freezes; no new base table is needed.
- `/api/v1/recovery` and the Returns & counts screen provide shipment-linked return drafts, approval, receiving, cycle/full counts, blind quantity entry, submission, approval, posting and cancellation. The API lists only shipped physical items as return choices.
- Return quantities are checked against physical shipment issues and other approved/received returns. Good returns restock the shipping location; damaged returns enter quarantine. Return ledger entries link to the original shipment issue, and repeated receipt keys cannot restock twice.
- Starting a count snapshots stock and freezes its scope. A cycle count freezes selected items; a full count freezes the location. The PostgreSQL ledger trigger rejects stock movements across existing posting paths while the scope is active. Submitting and approving do not alter stock. Posting creates linked adjustment entries and FIFO layer changes, then releases the freeze; cancellation also releases it.
- The local workspace uses a synthetic actor for approval while authentication is deferred. Batch/lot/serial/expiry-tracked returns and counts are blocked until their specific evidence is modeled. Virtual kit return intake is not offered because the shipped physical stock is in components.

## Stage 3H POS preparation evidence

- SQL 009 adds `sales_source` to the existing `cims.sales_order` table, defaults existing orders to manual retail, indexes source-filtered lists, and rejects a POS order status beyond draft/cancelled. No POS base table is needed for the cart preparation.
- Sales-order drafts now record manual retail, wholesale or POS source. The `/sales/orders?source=POS` filter lists POS carts. Source is immutable after creation, and POS confirmation fails with `pos_checkout_unavailable` before any reservation.
- `/pos/walk-in-customer` reuses or creates a tenant-scoped B2C walk-in customer for a sales location. The POS screen searches active items by barcode, SKU or name, supports scanner Enter for exact codes, saves/reopens priced draft carts, and can discard a draft.
- Final checkout is deliberately unavailable: the required single operation across order, stock, invoice and payment cannot be verified without the separate billing/payment system's interface. Shift opening/closing, settlement reconciliation, receipts and POS returns through the financial side remain pending.

## Stage 3I mobile and offline evidence

- SQL 010 creates `cims.mobile_operation_receipt`, the only new base table for offline synchronization. Accepted receipts are tenant-scoped, audited and immutable. Existing business services own the underlying documents and stock effects.
- `MobileService` checks the actor, operation ID, request hash, predecessor receipts and document version. A domain action and its receipt commit in one transaction. A repeated request returns the original result; a different payload under the same ID conflicts.
- The mobile workspace saves receive, pick, single-package pack, transfer receipt, full count and POS draft actions in IndexedDB under a tenant/actor scope. It supports camera barcode scan where the browser provides `BarcodeDetector`, plus keyboard/Bluetooth input, and keeps reference documents for local mode after reload.
- The queue orders dependencies, shows accepted/conflict/blocked states, and supports retry, edit and discard. A lost POST response is resolved through receipt lookup. Server acceptance time is authoritative; device capture time remains metadata.
- Sync calls existing services, which recheck permission, availability, freezes and quantities. POS drafts are repriced and retaxed by the server, and never reserve, invoice, ship or record payment. The authenticated sign-out path warns before discarding unsynced drafts and clears local cached data.

## Stage 3J operational reports and pilot tooling

- `/api/v1/reports/overview` and the Reports tab show tenant-scoped counts of active items, stock positions, saleable positions at/below safety stock, open purchase orders, confirmed sales orders, active counts and waiting reconciliation jobs.
- Whole-business totals use the existing inventory authorization gate: local mode or authenticated Owner/Admin. Other authenticated roles fail closed until location permissions are mapped. Stock quantity is deliberately not summed across unrelated items and units.
- The Reports tab reuses the inventory read side for per-item/location stock quantities, valuation completeness and paged ledger movements. New routes provide paged remaining FIFO receipt layers with age and value, sales-order summaries by status/source, purchase-order summaries by status/currency and confirmed-order tax estimates. Date filters are validated. These are order snapshots, not invoice, payment or filed-tax totals.
- `scripts/ops/backup-cims.ps1` produces a non-overwriting custom-format archive and SHA256; `scripts/ops/restore-cims-rehearsal.ps1` accepts only a fresh `cims_restore_*` database on the verified disposable port 55439. `scripts/sql/verify_cims_ledger.sql` compares four balance states with ledger totals. The [pilot runbook](PILOT_RUNBOOK.md) records daily checks and release gates.
- A disposable backup/restore rehearsal restored 119 `cims` tables; source and restored counts matched for 155 items, 360 ledger entries and 19 mobile receipts. Both copies had four ledger differences in the synthetic test dataset. A zero-difference rehearsal on representative pilot data is still required.
- `scripts/dev/measure-stock-api.py` ran 500 stock-read requests at 50 concurrent calls against the isolated test database: 0 errors, p50 168.03 ms, p95 189.94 ms on a 20-logical-CPU Intel64 machine with Python 3.14.3. The selected tenant had only three stock positions, so this verifies the harness but does not satisfy the representative-data workload gate.
- Billing, payments, invoices, tax reporting and financial aging remain with the separate system. MFA and custom roles depend on the deferred authenticated foundation.

## Verification and boundaries

Stage 3I and the Stage 3J operational increment verification: **163 backend tests passed, 2 deferred foundation checks skipped; 39 frontend tests passed; TypeScript check and production build passed.** Ruff checks passed for changed backend files and the load harness. The existing frontend `lint` script cannot run because the project has no ESLint 9 flat configuration. Tests used synthetic records only in the disposable PostgreSQL server; live business records were not changed.

- Native opening-stock checks include exactly-once concurrent posting, two-state quantities, weighted average, FIFO, full rollback after a late line failure, immutable posted documents/ledger, stale drafts, changed locations, operational-history blocking, CSV dry-run/draft semantics, and worker reconciliation/backoff.
- HTTP acceptance exercises catalog setup/variants/kits/import, then opening draft/review/post/retry, balances, history, reconciliation and opening CSV drafts.
- Native procurement checks cover ten ordered/eight received, zero accounting movement, two outstanding, receipt replay, excess rejection, approval and amendment, FIFO layers, quarantine ineligible for sales, forbidden office receiving, posted-line immutability and outbox reconciliation. The sample purchase-order PDF was rendered and visually inspected.
- Native 3C checks cover customer CSV preview/import, reserve three from ten, full rollback after a later line fails, competing reservations for the last unit, concurrent credit-limit checks, cancellation after partial consumption, kit availability and immutable reservation quantities after kit edits. An HTTP check covers order creation, confirmation, availability and customer CSV template.
- Native 3D checks cover wrong and excess scans, override evidence, exact packing, partial shipment stock effects, duplicate posting, multi-line rollback, FIFO layer consumption, kit component shipment, and database guards on shipped package and scan evidence. An HTTP check covers pick list, scan, pack, packing-slip PDF and ship posting.
- Native 3E checks cover 40 dispatch/38 receipt/two loss, preserved ordinary transfer value, retry safety, FIFO provenance, cross-GST-registration blocking, failed multi-line confirmation rollback, and database status guards. An HTTP check covers transfer setup, draft, confirmation, dispatch, receipt and list routes.
- Native 3G checks cover good and damaged return posting, duplicate receipt safety, shipment reversal links, a cycle-count freeze blocking packed shipment posting, cancellation releasing the freeze, and approved count adjustment posting exactly once. The screen tests cover SQL setup guidance and return draft payloads. A real browser walkthrough remains pending.
- Native 3H preparation checks cover walk-in customer reuse, POS source filtering, no reservation on draft save, blocked checkout, immutable source, database status guard and draft discard. The screen test covers barcode Enter and a POS-sourced draft payload.
- Native 3I checks cover receipt replay, lost-response lookup, actor and tenant isolation, predecessor blocking, stale versions, quantity conflicts and no second pick/cart effect. IndexedDB tests cover reload persistence, scope separation, dependency order, credential rejection, lost-response recovery and conflict blocking. A mobile view test saves and reloads a complete offline count.
- Native 3J overview checks compare operational counts with confirmed reservations, verify tenant isolation and reject a role without mapped whole-business access.
- Native 3J detailed-report checks cover FIFO layer quantity/value and aging, status/currency order groups, date filters, tax snapshots, tenant isolation and HTTP response contracts. Frontend checks cover location and date filtering.
- The packing-slip HTTP response was verified as PDF. A visual render was attempted, but automatic approval review rejected the temporary sample-generation command; visual PDF layout is not claimed.
- Browser automation was attempted but blocked by Windows desktop composition/geometry errors. Component tests and HTTP tests passed; a real browser walkthrough is not claimed. Camera scanning on a physical device remains unverified.
- The full authenticated foundation and its two PostgreSQL acceptance checks remain deferred.
- Purchase receiving and shipment posting currently change physical stock without the later accounting-side bill or invoice entries. The separate billing/invoice system's interface and responsibility for those entries have not been defined. Credit exposure currently includes confirmed uninvoiced sales commitments only.
- Transfers and loss settlement change physical stock without changing accounting quantity. The external accounting system's handling of a settled physical loss remains undefined. Draft edits/cancellation, tracked-item transfers and a named loss approver await later work. A real browser walkthrough of transfers has not been completed.
- Stage 3G intentionally stops at physical return/count operations. Quantity-linked and value-only credit notes, refunds, credit application and accounting value adjustments belong to the separate billing system; their interface remains undefined. Count approvals have no distinct named approver in local mode. The screen currently lists the first 100 active catalog items and recent shipped documents.
- POS cart drafts do not reserve stock, ship, invoice, record payment or issue a receipt. This code version requires SQL 009 before using the sales-order routes because `sales_source` is part of their response. The external billing/payment contract is still needed to finish 3H safely.
- Offline reference lists cache at most the first 100 records per paginated source. The current local workspace has no named user, so its queue uses the `local` actor; authenticated offline session restoration still needs work. The mobile pack screen creates one package for one order line. It does not post a shipment.
- Current environment: Python 3.14 and PostgreSQL 18. The plan's Python 3.12/PostgreSQL 16 combination remains unverified.

## Remaining work

1. SQL 002 is confirmed installed by a read-only query on the existing Inventroy database at port 5432. Apply SQL 003 through 010 manually in pgAdmin, in order. Perform a browser walkthrough with representative business data and visually review a packing slip.
2. Resume Chunk 1 schema mapping and authentication when requested. Establish a restricted live runtime role and authenticated tenant/location permissions before multi-user use. Non-Owner/Admin inventory access currently fails closed.
3. Define the interface to the separate billing/invoice system before completing Stage 3H checkout and the later workflows that require invoices, payments or refunds. Stage 3F invoice and bill creation remains excluded.
4. Add linked reversal documents before offering corrections to posted stock.
5. Extend FIFO issue/consumption integration to later outbound workflows such as transfers and internal issues. Shipment issues now record FIFO layer consumption.
6. Add the same ledger TRUNCATE guard to future partitions during database maintenance.
7. Complete remaining Stage 3J reports and English/Hindi support; reconcile representative pilot data to zero differences; repeat backup/restore and the 50-user workload on that dataset. Authentication, custom roles and MFA remain deferred. The missing ESLint 9 configuration also needs repair before a clean lint gate.

See [README](../README.md) for SQL links, startup, worker commands and test isolation details.


