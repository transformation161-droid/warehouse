# Controlled PR40 migration on the stopped LOCAL restore from Test-ManagedPostgresBackup.ps1.
# Never connects to live Amvera, never creates or replaces a backup, never opens a port.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$RestoreDirectory,
    [Parameter(Mandatory=$true)][string]$RestoreContainer,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedSha,
    [string]$Image = 'ksu-managed:pr39'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$sql = Join-Path $root 'server\postgres\005_inventory_count.sql'
$check = Join-Path $PSScriptRoot 'inventory_migration_stage.py'
$restore = [IO.Path]::GetFullPath($RestoreDirectory)
$adminEnv = Join-Path $restore 'local-admin.env'
$restoreReport = Join-Path $restore 'restore.json'
$expectedName = '^ksu-managed-restore-[a-f0-9]{32}$'
if ($RestoreContainer -cnotmatch $expectedName) {
    throw 'Only a retained independent-restore container name is permitted'
}
foreach ($path in @($sql, $check, $adminEnv, $restoreReport)) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required local file is missing: $path" }
    $file = Get-Item -LiteralPath $path
    if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Symlink/reparse files are not allowed'
    }
}
$evidence = Get-Content -Raw -LiteralPath $restoreReport | ConvertFrom-Json
if ($evidence.verified -ne $true -or
    $evidence.sessions_revoked -ne $true -or
    $evidence.package_sha256 -cnotmatch '^[a-f0-9]{64}$') {
    throw 'This directory does not contain a verified independent restore report'
}

$revision = (& git -C $root rev-parse HEAD)
if ($LASTEXITCODE -ne 0 -or $revision.Trim() -cne $ExpectedSha) {
    throw 'Source checkout HEAD differs from the approved PR40 full commit SHA'
}
$dirty = (& git -C $root status --porcelain -- tools/inventory_migration_stage.py tools/Test-InventoryMigration.ps1 server/postgres/005_inventory_count.sql)
if ($LASTEXITCODE -ne 0 -or $dirty) {
    throw 'Migration checker or SQL is modified; refusing to run'
}

# Inspect the real Docker container identity; do not rely on the name alone.
$raw = (& docker inspect $RestoreContainer)
if ($LASTEXITCODE -ne 0 -or !$raw) { throw 'Independent restore container not found' }
$detail = ($raw | ConvertFrom-Json)[0]
if ($detail.State.Running -ne $false) { throw 'Restore container must be stopped before this test' }
if ($detail.Config.Image -notmatch '^postgres:17-bookworm@sha256:') {
    throw 'Unexpected Docker image; production container is not permitted'
}
if (@($detail.Mounts | Where-Object { $_.Name -eq "$RestoreContainer-data" -and
       $_.Destination -eq '/var/lib/postgresql/data' }).Count -ne 1) {
    throw 'Independent restore data volume identity mismatch'
}
if (@($detail.Mounts | Where-Object { $_.Name -eq "$RestoreContainer-tls" -and
       $_.Destination -eq '/private-tls' }).Count -ne 1) {
    throw 'Independent restore TLS volume identity mismatch'
}
if ($detail.HostConfig.PortBindings -and @($detail.HostConfig.PortBindings.PSObject.Properties).Count) {
    throw 'The restore must have no published ports'
}
& docker image inspect $Image *> $null
if ($LASTEXITCODE -ne 0) { throw "Approved local application image is absent: $Image" }

Write-Host 'Running migration against the already-verified PRIVATE local restore only.'
$started = $false
try {
    & docker start $RestoreContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restart isolated restore PostgreSQL' }
    $started = $true
    $ready = $false
    for ($n=0; $n -lt 45; $n++) {
        & docker exec $RestoreContainer pg_isready -U postgres -d ksu_restore *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (!$ready) { throw 'Isolated restore PostgreSQL not ready' }
    $args = @(
        'run','--rm','--pull','never',
        '--network',"container:$RestoreContainer",
        '--cpus','0.5','--memory','1g','--memory-swap','1g','--read-only',
        '--security-opt','no-new-privileges','--cap-drop','ALL',
        '--env-file',$adminEnv,
        '--mount',"type=volume,src=$RestoreContainer-tls,dst=/config,readonly",
        '--mount',"type=bind,src=$sql,dst=/input/inventory.sql,readonly",
        '--mount',"type=bind,src=$check,dst=/input/check.py,readonly",
        '--entrypoint','python',
        $Image,'/input/check.py','/input/inventory.sql'
    )
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Local SQL migration acceptance FAILED' }
    Write-Host 'SQL migration tested on independent restore. Production PostgreSQL untouched.'
}
finally {
    if ($started) {
        & docker stop $RestoreContainer | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Could not stop the isolated test container; check Docker state'
        }
    }
}
