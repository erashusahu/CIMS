# CIMS pilot runbook

This runbook covers the current local CIMS workspace. The authenticated foundation, external billing connection and financial settlement are not ready for a multi-user pilot.

## Before opening the workspace

1. Confirm the target PostgreSQL database and port. The live business database is `Inventroy` on port `5432`; the disposable verification cluster uses port `55439`.
2. Apply the extension scripts listed in [README](../README.md) in order. SQL 010 is required for mobile synchronization. Keep a backup before applying any future migration.
3. Check `GET /health` for the API process. `GET /ready` currently checks the deferred authentication foundation and may return 503 in local mode; use the feature setup indicators and the permission-gated Reports overview for operational checks.
4. Confirm the reconciliation queue is processing and the Reports overview shows the expected waiting-job count. Review `/api/v1/inventory/reconciliation` for the selected business before a pilot.

## Daily checks

- Review stock positions, valuation completeness, recent ledger movements, FIFO aging, and reconciliation jobs in **Reports**.
- Treat any incomplete FIFO valuation or unexplained ledger difference as a blocker for stock-value decisions.
- Use sales and purchasing summaries as **order snapshots**. They are not invoice, payment, payable or filed-tax reports.
- Export logs around a failed request ID. The API logs failures with request context; do not paste credentials or customer details into support tickets.

## Backup and restore rehearsal

Use PostgreSQL's `PGPASSWORD` environment variable or a local password file. Do not put a password on the command line or in this document. The backup script refuses to overwrite an existing archive.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ops\backup-cims.ps1 `
  -Database Inventroy -Username <database_user> `
  -Destination .\backups\cims-YYYYMMDD.backup -Server 127.0.0.1 -Port 5432
```

The script checks that `pg_restore` can read the archive and prints its SHA256 hash. Rehearse restoration only into the verified disposable cluster on port `55439`; the restore script refuses to target the live database and requires a fresh database name beginning `cims_restore_`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ops\restore-cims-rehearsal.ps1 `
  -Archive .\backups\cims-YYYYMMDD.backup -TargetDatabase cims_restore_pilot
```

After restoration, compare source and restored row counts for key tables and run the read-only [ledger check](../scripts/sql/verify_cims_ledger.sql) on both databases. **A readable archive is not a passed recovery rehearsal.** Investigate every mismatch, test the restored application against the isolated database, and record recovery time and operator steps.

On 2026-11-06, a rehearsal of the disposable test database restored 119 `cims` tables. Source and restored counts matched for 155 items, 360 ledger entries and 19 mobile receipts. The ledger check reported four mismatches in both databases. The synthetic test cluster contains records from guard and failure-path tests; this result does not establish a clean pilot baseline. A zero-mismatch rehearsal on representative pilot data remains required.

## Pilot journeys and release gates

Run the business journeys from PLAN (28): catalog and opening stock, purchase receiving, sales reservation, pick/pack/ship, transfer, physical return, count correction and POS draft. Record each document ID and verify the matching stock ledger effect. Invoice, payment, credit-note and POS checkout steps require the separately developed billing system and its agreed integration.

Before a multi-user pilot, finish authentication and location grants, custom roles, MFA and recovery, and validate the billing connection. Run a 50-concurrent-user workload on representative data; record hardware, dataset size, API route, request count, p50/p95 latency and errors. The plan's stock-query p95 target is below 300 ms. The guarded measurement command is:

```powershell
$env:CIMS_CATALOG_TEST_URL='postgresql+asyncpg://cims_catalog_test_app@127.0.0.1:55439/Inventroy'
& .\.venv\Scripts\python.exe scripts\dev\measure-stock-api.py --requests 500 --concurrency 50
```

On 2026-11-06, this command returned 500 successful stock API requests with 50 concurrent calls, p50 168.03 ms and p95 189.94 ms on a 20-logical-CPU Intel64 Family 6 Model 151 machine with Python 3.14.3 and PostgreSQL 18. The selected synthetic tenant had only **three stock positions**. This is a harness check, not a representative-data pass; production-scale performance remains an open release gate.
