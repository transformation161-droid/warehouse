# Local demo launcher. Never deletes containers, volumes, or existing databases.
[CmdletBinding()]
param(
    [string]$Source = 'C:\KSU\demo-data\backups\check1.sqlite3',
    [ValidateSet('Start','Stop')][string]$Action = 'Start',
    [string]$AppImage = 'ksu-postgres:offline',
    [int]$Port = 8002
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $project 'var/windows-postgres.json'
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed. Existing data is retained; inspect containers locally.' }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop and select Linux containers.' }
$os = Invoke-Docker info --format '{{.OSType}}'
if ($os -ne 'linux') { throw 'Start Docker Desktop with Linux containers.' }
function Wait-Database {
    for ($n=0; $n -lt 60; $n++) {
        & docker exec $dbName pg_isready -h 127.0.0.1 -p 5432 -U postgres *> $null
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw 'Local PostgreSQL did not become ready. Existing data retained.'
}
function Wait-App {
    for ($n=0; $n -lt 60; $n++) {
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:$Port/api/health" -TimeoutSec 2
            if ($health.status -eq 'ok') { Write-Host "Open http://localhost:$Port ; sign in with your existing manager / inspector passwords."; return }
        } catch { }
        Start-Sleep -Seconds 1
    }
    throw 'Application did not become healthy. Data retained; inspect docker logs locally.'
}
if (Test-Path -LiteralPath $stateFile) {
    $state = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
    $dbName = $state.db; $appName = $state.app; $Port = [int]$state.port
    if ($dbName -notmatch '^ksu-windows-[a-f0-9]{12}$' -or $appName -ne "$dbName-app") { throw 'Invalid state file.' }
    # Never provision or migrate a second time, including after a failed first attempt.
    if ($Action -eq 'Stop') { Invoke-Docker stop $appName $dbName | Out-Null; return }
    if (-not $state.ready) { throw 'Previous setup incomplete. No automatic retry or overwrite. Inspect retained containers and volumes.' }
    Invoke-Docker start $dbName | Out-Null
    Wait-Database
    Invoke-Docker start $appName | Out-Null
    Wait-App
    return
}
if ($Action -eq 'Stop') { throw 'No saved local installation.' }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Invalid local port.' }
$sourceFile = Get-Item -LiteralPath $Source
if ($sourceFile.PSIsContainer) { throw 'Select an independent SQLite backup file.' }
foreach ($suffix in @('-wal','-shm','-journal')) {
    if (Test-Path -LiteralPath ($sourceFile.FullName+$suffix)) { throw 'Use a closed standalone SQLite backup without WAL/journal sidecars.' }
}
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Port)
try { $listener.Start() } finally { $listener.Stop() }
$pgImage = 'postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
$id = [Guid]::NewGuid().ToString('N').Substring(0,12)
$dbName = "ksu-windows-$id"
$appName = "$dbName-app"
$dbVolume = "$dbName-data"
$workVolume = "$dbName-work"
$networkName = "$dbName-net"
$keys = @('POSTGRES_PASSWORD','KSU_PG_ADMIN_DSN','KSU_PG_RUNTIME_PASSWORD','KSU_DATABASE_URL','KSU_ORIGIN','KSU_SECURE_COOKIES','KSU_WORK_DIR','KSU_PG_RESTORE_DIR','KSU_PG_MIGRATION_DIR','KSU_REQUIRE_INITIALIZED','KSU_PG_BACKUP_PASSWORD','KSU_PG_BACKUP_DSN','KSU_PG_BACKUP_DIR')
$old = @{}
foreach ($key in $keys) { $old[$key] = [Environment]::GetEnvironmentVariable($key) }
function New-ServiceSecret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes); return [Convert]::ToBase64String($bytes) } finally { $rng.Dispose() }
}
try {
    # --pull never makes the recovery path usable without Internet and prevents implicit downloads.
    Invoke-Docker image inspect $pgImage | Out-Null
    Invoke-Docker image inspect $AppImage | Out-Null
    $codeMount = @('--mount',"type=bind,src=$project,dst=/app,readonly")
    # Validate the offline runtime and preserved demo accounts before allocating data volumes.
    $probe = "import sqlite3, subprocess, psycopg, uvicorn; from server.pg_admin import migrate_sqlite; assert subprocess.check_output(['pg_dump','--version']).decode().startswith('pg_dump (PostgreSQL) 17.'); c=sqlite3.connect('file:/input/source.sqlite3?mode=ro&immutable=1',uri=True); assert {r[0] for r in c.execute('select username from users where active=1')} >= {'manager','inspector'}"
    Invoke-Docker run --rm --pull never --network none @codeMount `
        --mount "type=bind,src=$($sourceFile.FullName),dst=/input/source.sqlite3,readonly" `
        --entrypoint python $AppImage -c $probe
    $env:POSTGRES_PASSWORD = New-ServiceSecret
    $env:KSU_PG_RUNTIME_PASSWORD = New-ServiceSecret
    $env:KSU_PG_BACKUP_PASSWORD = New-ServiceSecret
    $env:KSU_PG_ADMIN_DSN = "host=127.0.0.1 port=5432 dbname=postgres user=postgres password=$env:POSTGRES_PASSWORD"
    $env:KSU_DATABASE_URL = "host=127.0.0.1 port=5432 dbname=postgres user=ksu_runtime password=$env:KSU_PG_RUNTIME_PASSWORD"
    $env:KSU_PG_BACKUP_DSN = "host=127.0.0.1 port=5432 dbname=postgres user=ksu_backup password=$env:KSU_PG_BACKUP_PASSWORD"
    $env:KSU_ORIGIN = "http://localhost:$Port"
    $env:KSU_SECURE_COOKIES = '0'
    $env:KSU_WORK_DIR = '/work/app'
    $env:KSU_PG_BACKUP_DIR = '/work/backups'
    $env:KSU_PG_MIGRATION_DIR = '/work'
    $env:KSU_REQUIRE_INITIALIZED = '1'
    New-Item -ItemType Directory -Force -Path (Split-Path $stateFile) | Out-Null
    $state = @{db=$dbName;app=$appName;network=$networkName;port=$Port;ready=$false;sourceSha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash}
    # CreateNew reserves the installation before the first persistent Docker operation.
    $stream = [IO.File]::Open($stateFile,[IO.FileMode]::CreateNew)
    try { $bytes=[Text.Encoding]::UTF8.GetBytes(($state | ConvertTo-Json)); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
    Invoke-Docker network create --opt com.docker.network.bridge.enable_icc=false $networkName | Out-Null
    Invoke-Docker run -d --pull never --network $networkName --name $dbName --memory 1g --cpus 0.5 -e POSTGRES_PASSWORD `
        -p "127.0.0.1:${Port}:8000" -v "${dbVolume}:/var/lib/postgresql/data" $pgImage | Out-Null
    Wait-Database
    Invoke-Docker run --rm --pull never --network none --user 0 -v "${workVolume}:/work" `
        --entrypoint python $AppImage -c "import os; os.chown('/work',1000,1000); os.makedirs('/work/backups',mode=0o700,exist_ok=True); os.chown('/work/backups',1000,1000); os.chmod('/work/backups',0o700)"
    Invoke-Docker run --rm --pull never --network "container:$dbName" -e KSU_PG_ADMIN_DSN -e KSU_PG_RUNTIME_PASSWORD -e KSU_PG_BACKUP_PASSWORD `
        @codeMount --entrypoint python $AppImage -m server.pg_admin provision-roles --runtime-role ksu_runtime --backup-role ksu_backup
    Invoke-Docker run --rm --pull never --network "container:$dbName" -e KSU_PG_ADMIN_DSN @codeMount `
        --entrypoint python $AppImage -m server.pg_admin init --runtime-role ksu_runtime --backup-role ksu_backup
    $report = Invoke-Docker run --rm --pull never --network "container:$dbName" -e KSU_PG_ADMIN_DSN -e KSU_PG_MIGRATION_DIR `
        @codeMount --mount "type=bind,src=$($sourceFile.FullName),dst=/input/source.sqlite3,readonly" `
        -v "${workVolume}:/work" --entrypoint python $AppImage -m server.pg_admin migrate-sqlite /input/source.sqlite3
    $verified = ($report -join "`n") | ConvertFrom-Json
    if (-not $verified.source_read_only -or -not $verified.sessions_revoked -or -not $verified.tables.users) { throw 'Migration verification report missing.' }
    $report | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $project 'var/windows-postgres-migration.json')
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash -ne $state.sourceSha256) { throw 'Source file changed. Application will not start.' }
    Invoke-Docker run -d --pull never --name $appName --network "container:$dbName" --memory 1g --cpus 0.5 `
        -e KSU_DATABASE_URL -e KSU_ORIGIN -e KSU_SECURE_COOKIES -e KSU_WORK_DIR -e KSU_REQUIRE_INITIALIZED -e KSU_PG_BACKUP_DSN -e KSU_PG_BACKUP_DIR @codeMount -v "${workVolume}:/work" $AppImage | Out-Null
    Wait-App
    $state.ready = $true
    $state | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $stateFile
    Write-Host 'Migration verified: counts and SHA-256 per table are in var/windows-postgres-migration.json.'
    Write-Host 'Restart: run this script again. Stop: run this script with -Action Stop.'
    Write-Host "Stop: docker stop $appName $dbName"
    Write-Host "Local data volumes retained: $dbVolume ; $workVolume"
} finally {
    foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $old[$key]) }
}
