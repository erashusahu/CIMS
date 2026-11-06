# CIMS — Inventory Management System

Local React/TypeScript + Vite frontend, FastAPI + SQLAlchemy async backend, and PostgreSQL. No Docker is required.

## Project layout

| Folder | Purpose |
|---|---|
| `backend/app` | API routes, schemas, database mappings and business services. |
| `backend/tests` | Focused and PostgreSQL acceptance tests. |
| `frontend/src/features` | Screens and tests grouped by business area. |
| `scripts/dev` | Local checks, OpenAPI export and isolated test tools. |
| `scripts/ops` | Worker, backup and restore rehearsal commands. |
| `scripts/sql` | Ordered database extensions and read-only ledger verification. |
| `docs` | Implementation status and the pilot runbook. |

`.tmp`, `.venv`, `frontend/node_modules` and build caches are local working files. Keep them out of source control.

## Current scope

- Chunk 2 catalog is implemented: items, variants, virtual kits, units/conversions, prices/tax, configuration, and CSV dry-run/import.
- Chunk 3A is implemented for local use: opening-stock draft/review/post, CSV drafts, balances, movement history, valuation, reconciliation, and a PostgreSQL-backed local worker.
- Authentication is deferred at the user's request. The explicit local workspace mode works only in development on loopback with origin checks.
- Chunk 3B procurement is implemented: suppliers, reorder rules and suggestions, purchase orders and approval, partial physical receiving, quarantine, and printable PDFs.
- Chunk 3C is implemented: customers and CSV import, sales drafts and confirmation, per-location availability, credit checks, atomic stock reservations and cancellation. See [implementation status](docs/IMPLEMENTATION_STATUS.md) for evidence and limits.
- Chunk 3D is implemented: pick lists and scan verification, packages and packing slips, partial shipments, and atomic reservation consumption with physical stock issue.
- Chunk 3E is implemented: same-GST-registration location transfers, source reservations, in-transit stock, partial receipts, FIFO provenance, and reasoned physical loss settlement.
- Stage 3G physical operations are implemented: shipment-linked returns, good-stock restocking, damaged-stock quarantine, cycle/full and blind counts, count freezes, and approved quantity adjustments. Credit notes, refunds and accounting value adjustments remain with the separate billing system.
- Stage 3H POS preparation is available: source-tagged sales drafts, barcode/SKU cart search, walk-in customer setup, and draft discard. Final checkout, shifts, receipts and cash reconciliation await the separate billing and payment interface. POS drafts cannot reserve or ship stock.
- Stage 3I mobile capture is implemented for the local workspace: receive, pick, pack, transfer receipt, count and POS drafts persist in an actor/tenant-scoped IndexedDB queue. Synchronization checks document versions and uses immutable server receipts to recover accepted operations after a lost response. Apply SQL 010 before syncing on the live database.
- Stage 3J reporting now includes permission-gated operational counts, stock and movement tables, FIFO receipt-layer aging, sales and purchase order summaries, and confirmed-order tax estimates. [Pilot runbook](docs/PILOT_RUNBOOK.md) and guarded backup/restore rehearsal commands are included. Financial reports and security completion remain pending.
- Billing and invoices are developed separately and are outside CIMS. Stage 3F in the original plan is superseded for those features; any accounting, credit, payment or Tally connection to that system needs an agreed interface.

## Existing database setup

The ignored root `.env` holds the configured connection to `localhost:5432/Inventroy`. Keep credentials there. Do not overwrite this file when following setup examples.

The existing application tables are in schema `cims`. Do not apply the scaffold's `0001_foundation` Alembic migration: it creates a different foundation in `public`.

SQL 002 has been confirmed installed in **Inventroy**. Apply the remaining scripts yourself in pgAdmin, in this order:

1. [003_cims_opening_stock.sql](scripts/sql/003_cims_opening_stock.sql): creates opening-stock headers/lines and the outbox, adds its document kind, RLS, audit and immutability guards.
2. [004_cims_procurement_guards.sql](scripts/sql/004_cims_procurement_guards.sql): blocks changes to posted purchase-receipt lines. The existing `cims` schema already contains supplier, order, receipt, reorder, customer and address tables.
3. [005_cims_sales_reservation_guards.sql](scripts/sql/005_cims_sales_reservation_guards.sql): protects confirmed sales-order details and the component quantities saved in reservations. The existing `cims.sales_order`, `cims.sales_order_line` and `cims.reservation` tables supply the base schema.
4. [006_cims_picking_guards.sql](scripts/sql/006_cims_picking_guards.sql): creates the missing `cims.pick_scan` table and protects shipped package and shipment details. Existing package and shipment tables are reused.
5. [007_cims_transfer_extension.sql](scripts/sql/007_cims_transfer_extension.sql): adds transfer-operation evidence and guards to existing transfer orders and lines.
6. [008_cims_returns_counts_guards.sql](scripts/sql/008_cims_returns_counts_guards.sql): freezes counted stock at the ledger, protects count and return documents, and reuses the existing return/count/adjustment tables.
7. [009_cims_pos_draft_source.sql](scripts/sql/009_cims_pos_draft_source.sql): adds a sales-source column and guards POS carts as drafts. No POS base table is needed for the draft workflow.
8. [010_cims_mobile_operation_receipts.sql](scripts/sql/010_cims_mobile_operation_receipts.sql): creates the missing `cims.mobile_operation_receipt` table for accepted offline operations, with tenant isolation, audit and immutability guards. Run after 003–009.

Use **Execute script** for the complete file, including `BEGIN` and `COMMIT`. Executing only the statement under the cursor may run `COMMIT` without the preceding changes. Script 003 deliberately stops if its tables already exist; inspect the existing definitions rather than rerunning it blindly.

Authentication script 001 is not needed for the local catalog mode. No extension script was applied to your existing server by the implementation work.

Verify the catalog columns after executing script 002:

```sql
SELECT current_database(), inet_server_port();
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'cims' AND table_name = 'item'
  AND column_name IN ('catalog_version', 'cost_center_id');
```

The UI shows a setup message while the required database extension is missing.

## Start locally

Dependencies are installed in `.venv` and `frontend/node_modules` in this workspace. For a fresh checkout, use Python 3.12+, install `backend/requirements-dev.txt`, and run `npm.cmd --prefix frontend ci`. The current checks ran on Python 3.14 and PostgreSQL 18; Python 3.12/PostgreSQL 16 are not yet verified.

Root `.env`:

```dotenv
APP_ENV=development
CATALOG_LOCAL_MODE=true
FRONTEND_ORIGIN=http://127.0.0.1:5174
# Keep the existing private DATABASE_URL and JWT_SECRET.
```

Frontend `frontend/.env`:

```dotenv
VITE_CATALOG_LOCAL_MODE=true
VITE_API_URL=http://127.0.0.1:8000/api/v1
```

From repository root, start the API and frontend in separate terminals:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
npm.cmd --prefix frontend run dev -- --host 127.0.0.1 --port 5174 --strictPort
```

Open http://127.0.0.1:5174. Select or create a local business workspace. Set up units and item types, then catalog items. Under **Stock → Opening stock**, create a storage location, save a draft, review converted stock quantities and costs, then post explicitly.

Use **Purchasing** for suppliers, reorder rules, purchase orders, approval, physical receiving and quarantine. Use **Customers** for buyers, addresses and CSV import. Use **Sales orders** to save a draft, review stock and credit, and confirm it. The confirmed order shows a pick list: scan or type an item SKU/barcode, record package contents and dimensions, review the packing slip, then choose **Ship and post stock**. Packing does not reduce stock. Shipping reduces physical stock and consumes its reservation together. A customer's credit limit must cover the order total to confirm it. Opening costs are INR per **stock unit**, even when the entered quantity uses cartons. Opening CSV import creates a reviewable draft; it never posts automatically. Opening is rejected after operational movements begin for an item/location. Posted documents cannot be edited. A reversal/correction workflow is still future work.

Use **Transfers** to add a GST registration, map the source and destination storage locations to that same registration, create a draft, confirm the source reservation, dispatch to its dedicated transit location, then record partial receipts. Goods still in transit remain visible. An Owner/Admin can settle a physical loss with a reason. Batch, lot, serial and expiry-tracked items are blocked until transfer tracking is built. A location's state code must match the first two digits of its GSTIN; an empty state code is filled when mapped. Existing different state codes need correction before mapping.

Opening stock inserts physical and accounting ledger entries atomically. Purchase receiving and sales shipping insert physical entries only. Later accounting-side bill and invoice entries belong to the separate billing/invoice system; its connection to CIMS is not yet defined. Existing database triggers update quantities; application code does not update them a second time. FIFO reports use remaining receipt layers, not the legacy weighted-average balance value.

## Local worker

Use the selected workspace's tenant UUID, obtainable from the local-workspaces API response:

```powershell
.\.venv\Scripts\python.exe scripts/ops/run-worker.py --tenant <tenant-uuid> --loop
```

The worker claims committed events using PostgreSQL row locks, checks affected stock quantities against the ledger, and records success or a retry with backoff. It uses the same root database configuration and performs database writes. It is not started automatically.

## Verification

```powershell
# Read-only metadata connection check against the configured database:
.\.venv\Scripts\python.exe scripts/dev/check-postgres.py

# Backend unit checks (native acceptance tests skip without their isolated fixture):
Set-Location backend
..\.venv\Scripts\python.exe -m pytest -q --disable-warnings

# Frontend, from repository root:
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build

# Generate API types without a running backend or database queries:
.\.venv\Scripts\python.exe scripts/dev/export-openapi.py
.\frontend\node_modules\.bin\openapi-typescript.cmd frontend/src/api/openapi.json -o frontend/src/api/openapi.d.ts
```

Native acceptance tests require a separate cluster at `127.0.0.1:55439`, admin `cims_test_admin`, database `Inventroy`, and a non-owner/NOBYPASSRLS login `cims_catalog_test_app`. They refuse the existing server's address/port. The guarded `scripts/dev/prepare-catalog-test.ps1` restores schema definitions only and applies 002–010 there; `-Reset` resets only that test database after verifying its data directory against `.tmp/catalog-test.json`. Never point these tests at your business database.

Set `CIMS_CATALOG_TEST_URL=postgresql+asyncpg://cims_catalog_test_app@127.0.0.1:55439/Inventroy` for the native checks. `scripts/dev/smoke-catalog.py` separately requires a test API on port 8001 that reports the exact isolated database identity before allowing HTTP mutations.

`/health` checks the process. `/ready` still checks the deferred authentication foundation and can return 503 while the local catalog works. Catalog and opening-stock setup checks are separate.

The live connection still uses a privileged PostgreSQL login. Restricted-role tenant isolation was tested in the disposable database, but authenticated multi-user production access remains unfinished.

