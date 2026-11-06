[CmdletBinding()]
param(
    [string]$PostgresBin = 'C:\Program Files\PostgreSQL\18\bin',
    [switch]$Reset
)

$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$taskSqlRoot = Join-Path $taskRoot 'scripts\sql'
$taskConfigPath = Join-Path $taskRoot '.tmp\catalog-test.json'
if (-not (Test-Path -LiteralPath $taskConfigPath)) { throw 'Initialize the isolated test cluster first.' }
$taskConfig = Get-Content -LiteralPath $taskConfigPath -Raw | ConvertFrom-Json
if ($taskConfig.port -ne 55439 -or $taskConfig.admin -ne 'cims_test_admin') {
    throw 'This script only supports the isolated catalog test server on port 55439.'
}
$taskPsql = Join-Path $PostgresBin 'psql.exe'
$taskCommon = @('-X', '-w', '-h', '127.0.0.1', '-p', '55439', '-U', 'cims_test_admin', '-v', 'ON_ERROR_STOP=1')
$taskActualData = & $taskPsql @taskCommon -d postgres -At -c 'SHOW data_directory'
if ($LASTEXITCODE -ne 0) { throw 'The isolated server is not running.' }
$taskExpectedData = [System.IO.Path]::GetFullPath((Join-Path $taskConfig.root 'data')).Replace([char]92,[char]47).TrimEnd('/')
if ([System.IO.Path]::GetFullPath($taskActualData.Trim()).Replace([char]92,[char]47).TrimEnd('/') -ne $taskExpectedData) {
    throw 'The server data directory does not match the temporary test cluster.'
}

# Export definitions only. No application data leaves the existing database.
$taskLine = Get-Content -LiteralPath (Join-Path $taskRoot '.env') | Where-Object { $_.StartsWith('DATABASE_URL=') } | Select-Object -First 1
$taskUri = [Uri]($taskLine.Substring(13).Replace('postgresql+asyncpg://','postgresql://'))
$taskAuth = $taskUri.UserInfo.Split(':',2)
$taskReference = Join-Path $taskRoot '.tmp\catalog-reference-schema.sql'
$env:PGPASSWORD = [Uri]::UnescapeDataString($taskAuth[1])
try {
    $taskExtensionJson = & $taskPsql -w -h $taskUri.Host -p $taskUri.Port -U $taskAuth[0] -d $taskUri.AbsolutePath.TrimStart('/') -At -c "SELECT coalesce(json_agg(json_build_object('name',e.extname,'schema',n.nspname)), '[]'::json) FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname <> 'plpgsql'"
    if ($LASTEXITCODE -ne 0) { throw 'Extension metadata could not be read.' }
    & (Join-Path $PostgresBin 'pg_dump.exe') -w -h $taskUri.Host -p $taskUri.Port -U $taskAuth[0] -d $taskUri.AbsolutePath.TrimStart('/') --schema-only --schema=cims --no-owner --no-privileges --file=$taskReference
    if ($LASTEXITCODE -ne 0) { throw 'Read-only reference-schema export failed.' }
} finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }

if ($Reset) {
    & (Join-Path $PostgresBin 'dropdb.exe') -w -h 127.0.0.1 -p 55439 -U cims_test_admin --force --if-exists Inventroy
    if ($LASTEXITCODE -ne 0) { throw 'The disposable test database could not be reset.' }
}
& (Join-Path $PostgresBin 'createdb.exe') -w -h 127.0.0.1 -p 55439 -U cims_test_admin Inventroy
if ($LASTEXITCODE -ne 0) { throw 'The isolated test database must be fresh.' }
$taskExtensionSql = @()
foreach ($taskExtension in ($taskExtensionJson | ConvertFrom-Json)) {
    $taskSchemaName = '"' + $taskExtension.schema.Replace('"','""') + '"'
    $taskExtensionName = '"' + $taskExtension.name.Replace('"','""') + '"'
    $taskExtensionSql += "CREATE SCHEMA IF NOT EXISTS $taskSchemaName;"
    $taskExtensionSql += "CREATE EXTENSION $taskExtensionName WITH SCHEMA $taskSchemaName;"
}
$taskExtensionFile = Join-Path $taskRoot '.tmp\catalog-test-extensions.sql'
[System.IO.File]::WriteAllText($taskExtensionFile, ($taskExtensionSql -join [Environment]::NewLine))
& $taskPsql @taskCommon -d Inventroy -q -f $taskExtensionFile
if ($LASTEXITCODE -ne 0) { throw 'Test extensions could not be installed.' }
# The extensions must exist in their original namespace before functions/tables restore.
$taskReferenceText = [System.IO.File]::ReadAllText($taskReference).Replace('CREATE SCHEMA cims;', 'CREATE SCHEMA IF NOT EXISTS cims;')
[System.IO.File]::WriteAllText($taskReference, $taskReferenceText)
& $taskPsql @taskCommon -d Inventroy -q -f $taskReference
if ($LASTEXITCODE -ne 0) { throw 'The test schema restore failed.' }
$taskFieldCount = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='cims' AND table_name='item' AND column_name IN ('catalog_version','cost_center_id')"
if ($taskFieldCount.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '002_cims_catalog_extension.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated catalog extension failed.' }
} elseif ($taskFieldCount.Trim() -ne '2') { throw 'The source database contains a partial catalog extension.' }

$taskOpeningTables = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='cims' AND table_name IN ('opening_stock','opening_stock_line','outbox_event')"
if ($taskOpeningTables.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '003_cims_opening_stock.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated opening-stock extension failed.' }
} elseif ($taskOpeningTables.Trim() -ne '3') { throw 'The source database contains a partial opening-stock extension.' }

$taskReceiptGuard = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM pg_trigger WHERE tgrelid='cims.purchase_receive_line'::regclass AND tgname='trg_guard_purchase_receive_line' AND NOT tgisinternal"
if ($taskReceiptGuard.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '004_cims_procurement_guards.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated procurement guard failed.' }
}

$taskSalesGuard = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM pg_trigger WHERE tgrelid='cims.reservation'::regclass AND tgname='trg_guard_reservation_snapshot' AND NOT tgisinternal"
if ($taskSalesGuard.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '005_cims_sales_reservation_guards.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated sales guard failed.' }
}

$taskPickTable = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='cims' AND table_name='pick_scan'"
if ($taskPickTable.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '006_cims_picking_guards.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated picking extension failed.' }
}

$taskTransferTable = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='cims' AND table_name='transfer_operation'"
if ($taskTransferTable.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '007_cims_transfer_extension.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated transfer extension failed.' }
}

$taskCountFreeze = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM pg_trigger WHERE tgrelid='cims.stock_ledger_entry'::regclass AND tgname='trg_count_freeze' AND NOT tgisinternal"
if ($taskCountFreeze.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '008_cims_returns_counts_guards.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated return/count guards failed.' }
}

$taskSalesSource = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.columns WHERE table_schema='cims' AND table_name='sales_order' AND column_name='sales_source'"
if ($taskSalesSource.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '009_cims_pos_draft_source.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated POS draft extension failed.' }
}

$taskMobileTable = & $taskPsql @taskCommon -d Inventroy -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='cims' AND table_name='mobile_operation_receipt'"
if ($taskMobileTable.Trim() -eq '0') {
    & $taskPsql @taskCommon -d Inventroy -q -f (Join-Path $taskSqlRoot '010_cims_mobile_operation_receipts.sql')
    if ($LASTEXITCODE -ne 0) { throw 'The isolated mobile operation extension failed.' }
}

$taskGrants = @'
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='cims_catalog_test_app') THEN
CREATE ROLE cims_catalog_test_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
END IF; END $$;
GRANT CONNECT ON DATABASE "Inventroy" TO cims_catalog_test_app;
GRANT USAGE ON SCHEMA cims TO cims_catalog_test_app;
GRANT SELECT ON ALL TABLES IN SCHEMA cims TO cims_catalog_test_app;
GRANT INSERT, UPDATE, DELETE ON cims.uom, cims.item_type, cims.item_group,
    cims.item_group_attribute, cims.item, cims.item_attribute_value,
    cims.uom_conversion, cims.composite_component TO cims_catalog_test_app;
GRANT INSERT ON cims.audit_log TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.opening_stock, cims.outbox_event, cims.idempotency_key,
    cims.number_sequence, cims.stock_balance, cims.movement_type,
    cims.location, cims.location_type TO cims_catalog_test_app;
GRANT INSERT, UPDATE, DELETE ON cims.opening_stock_line TO cims_catalog_test_app;
GRANT INSERT ON cims.stock_ledger_entry, cims.fifo_layer TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.vendor, cims.item_vendor TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.reorder_rule, cims.purchase_order,
    cims.purchase_receive, cims.purchase_order_line,
    cims.purchase_receive_line TO cims_catalog_test_app;
GRANT INSERT ON cims.document_status TO cims_catalog_test_app;
GRANT DELETE ON cims.purchase_order_line TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.customer, cims.party_address TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.sales_order, cims.sales_order_line,
    cims.reservation TO cims_catalog_test_app;
GRANT DELETE ON cims.sales_order_line TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.pick_scan, cims.package, cims.package_line,
    cims.shipment, cims.shipment_line, cims.shipment_package,
    cims.fifo_consumption, cims.fifo_layer TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.transfer_order, cims.transfer_order_line
    TO cims_catalog_test_app;
GRANT SELECT, INSERT ON cims.transfer_operation TO cims_catalog_test_app;
GRANT INSERT ON cims.tax_registration TO cims_catalog_test_app;
GRANT INSERT, UPDATE ON cims.sales_return, cims.sales_return_line,
    cims.audit_count, cims.audit_count_line, cims.adjustment TO cims_catalog_test_app;
GRANT INSERT ON cims.reason_code, cims.app_user TO cims_catalog_test_app;
GRANT SELECT, INSERT ON cims.mobile_operation_receipt TO cims_catalog_test_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA cims TO cims_catalog_test_app;
'@
$taskGrantFile = Join-Path $taskRoot '.tmp\catalog-test-grants.sql'
[System.IO.File]::WriteAllText($taskGrantFile, $taskGrants)
& $taskPsql @taskCommon -d Inventroy -q -f $taskGrantFile
if ($LASTEXITCODE -ne 0) { throw 'The restricted test role could not be configured.' }
Write-Output 'Prepared isolated test database on 127.0.0.1:55439. Existing server data was not changed.'
