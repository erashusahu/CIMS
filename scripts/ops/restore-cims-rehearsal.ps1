param(
    [Parameter(Mandatory=$true)][string]$Archive,
    [Parameter(Mandatory=$true)][ValidatePattern('^cims_restore_[a-z0-9_]+$')][string]$TargetDatabase
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$configPath = Join-Path $workspace '.tmp\catalog-test.json'
if (-not (Test-Path -LiteralPath $configPath)) { throw 'The isolated test-cluster identity file is missing.' }
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.port -ne 55439 -or $config.admin -ne 'cims_test_admin' -or $config.database -ne 'Inventroy') {
    throw 'Unexpected test-cluster identity.'
}
$root = [IO.Path]::GetFullPath($config.root)
$data = [IO.Path]::GetFullPath((Join-Path $root 'data'))
if (-not $data.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Unexpected test-cluster data path.'
}
$archivePath = [IO.Path]::GetFullPath($Archive)
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) { throw 'Backup archive not found.' }
$bin = 'C:\Program Files\PostgreSQL\18\bin'
$psql = Join-Path $bin 'psql.exe'
$createdb = Join-Path $bin 'createdb.exe'
$restore = Join-Path $bin 'pg_restore.exe'
foreach ($tool in @($psql, $createdb, $restore)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "PostgreSQL tool not found: $tool" }
}
$exists = & $psql --host=127.0.0.1 --port=55439 --username=cims_test_admin --dbname=postgres --no-psqlrc --tuples-only --no-align --command="SELECT 1 FROM pg_database WHERE datname = '$TargetDatabase'"
if ($LASTEXITCODE -ne 0) { throw 'Could not verify the isolated test cluster.' }
if (($exists | Out-String).Trim() -eq '1') { throw 'The target database already exists. Choose a fresh rehearsal name.' }
& $restore --list $archivePath | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The archive is unreadable.' }
& $createdb --host=127.0.0.1 --port=55439 --username=cims_test_admin $TargetDatabase
if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated rehearsal database.' }
& $restore --host=127.0.0.1 --port=55439 --username=cims_test_admin --dbname=$TargetDatabase --exit-on-error --no-owner --no-acl $archivePath
if ($LASTEXITCODE -ne 0) { throw "Restore failed; inspect isolated database $TargetDatabase." }
$check = & $psql --host=127.0.0.1 --port=55439 --username=cims_test_admin --dbname=$TargetDatabase --no-psqlrc --tuples-only --no-align --command="SELECT current_database(), count(*) FROM information_schema.tables WHERE table_schema='cims'"
if ($LASTEXITCODE -ne 0) { throw 'The restored database could not be queried.' }
Write-Output "Restored into isolated database $TargetDatabase on 127.0.0.1:55439."
Write-Output $check
Write-Output 'Run application readiness and ledger reconciliation against this isolated database before treating the backup as verified.'
