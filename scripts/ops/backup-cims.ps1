param(
    [Parameter(Mandatory=$true)][string]$Database,
    [Parameter(Mandatory=$true)][string]$Username,
    [Parameter(Mandatory=$true)][string]$Destination,
    [string]$Server = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 5432
)

$ErrorActionPreference = 'Stop'
$bin = 'C:\Program Files\PostgreSQL\18\bin'
$dump = Join-Path $bin 'pg_dump.exe'
$restore = Join-Path $bin 'pg_restore.exe'
if (-not (Test-Path -LiteralPath $dump) -or -not (Test-Path -LiteralPath $restore)) {
    throw 'PostgreSQL 18 command-line tools were not found.'
}
$target = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw 'The destination already exists. Choose a new archive path.' }
$parent = Split-Path -Parent $target
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
try {
    & $dump --host=$Server --port=$Port --username=$Username --dbname=$Database --format=custom --no-owner --no-acl --file=$target
    if ($LASTEXITCODE -ne 0) { throw 'pg_dump failed.' }
    & $restore --list $target | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The backup archive could not be read.' }
    $hash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    Write-Output "Backup archive: $target"
    Write-Output "SHA256: $hash"
    Write-Output 'Restore the archive into an isolated database and run reconciliation before using it for recovery.'
} catch {
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
    throw
}
