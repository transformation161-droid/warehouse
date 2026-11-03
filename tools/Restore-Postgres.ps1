# Offline, independent LOCAL restore. Requires Docker Desktop and preloaded images, no Python installation.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Backup,
    [string]$AppImage = 'ksu-postgres:offline',
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
$backupFile = Get-Item -LiteralPath $Backup
if ($backupFile.PSIsContainer) { throw 'Choose a backup file.' }
if ($AppImage -notmatch '^[A-Za-z0-9][A-Za-z0-9/_.:@-]+$') { throw 'Invalid local image name.' }
if ($backupFile.Extension -ne '.ksubackup') { throw 'Use a decrypted .ksubackup package from a trusted administrator.' }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Choose a local port from 1024 to 65535.' }
$pgImage = 'postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
$id = [Guid]::NewGuid().ToString('N').Substring(0,12)
$dbName = "ksu-offline-$id"
$appName = "$dbName-app"
$dbVolume = "$dbName-data"
$workVolume = "$dbName-work"
$keys = @('POSTGRES_PASSWORD','KSU_PG_ADMIN_DSN','KSU_PG_RUNTIME_PASSWORD','KSU_DATABASE_URL','KSU_ORIGIN','KSU_SECURE_COOKIES','KSU_WORK_DIR','KSU_PG_RESTORE_DIR')
$old = @{}
foreach ($key in $keys) { $old[$key] = [Environment]::GetEnvironmentVariable($key) }
function New-ServiceSecret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes); return [Convert]::ToBase64String($bytes) } finally { $rng.Dispose() }
}
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Local Docker operation failed. Inspect the local containers; no existing database was overwritten.' }
}
try {
    # --pull never makes the recovery path usable without Internet and prevents implicit downloads.
    Invoke-Docker image inspect $pgImage | Out-Null
    Invoke-Docker image inspect $AppImage | Out-Null
    $env:POSTGRES_PASSWORD = New-ServiceSecret
    $env:KSU_PG_RUNTIME_PASSWORD = New-ServiceSecret
    $env:KSU_PG_ADMIN_DSN = "host=127.0.0.1 port=5432 dbname=postgres user=postgres password=$env:POSTGRES_PASSWORD"
    $env:KSU_DATABASE_URL = "host=127.0.0.1 port=5432 dbname=postgres user=ksu_runtime password=$env:KSU_PG_RUNTIME_PASSWORD"
    $env:KSU_ORIGIN = "http://localhost:$Port"
    $env:KSU_SECURE_COOKIES = '0'
    $env:KSU_WORK_DIR = '/work/app'
    $env:KSU_PG_RESTORE_DIR = '/work'
    Invoke-Docker run -d --pull never --name $dbName --memory 1g --cpus 0.5 -e POSTGRES_PASSWORD `
        -p "127.0.0.1:${Port}:8000" -v "${dbVolume}:/var/lib/postgresql/data" $pgImage | Out-Null
    $ready = $false
    for ($n=0; $n -lt 60; $n++) {
        & docker exec $dbName pg_isready -h 127.0.0.1 -p 5432 -U postgres *> $null
        if ($LASTEXITCODE -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Local PostgreSQL did not become ready.' }
    Invoke-Docker run --rm --pull never --network none --user 0 -v "${workVolume}:/work" `
        --entrypoint chown $AppImage '1000:1000' /work
    Invoke-Docker run --rm --pull never --network "container:$dbName" -e KSU_PG_ADMIN_DSN -e KSU_PG_RUNTIME_PASSWORD `
        --entrypoint python $AppImage -m server.pg_admin provision-roles --runtime-role ksu_runtime
    Invoke-Docker run --rm --pull never --network "container:$dbName" -e KSU_PG_ADMIN_DSN -e KSU_PG_RESTORE_DIR `
        -v "$($backupFile.FullName):/input/backup.ksubackup:ro" -v "${workVolume}:/work" --entrypoint python $AppImage `
        -m server.pg_admin restore "/input/backup.ksubackup" --runtime-role ksu_runtime
    Invoke-Docker run -d --pull never --name $appName --network "container:$dbName" --memory 1g --cpus 0.5 `
        -e KSU_DATABASE_URL -e KSU_ORIGIN -e KSU_SECURE_COOKIES -e KSU_WORK_DIR -v "${workVolume}:/work" $AppImage | Out-Null
    Write-Host "Verified independent local restore. Open http://localhost:$Port and use the original account passwords."
    Write-Host "Stop: docker stop $appName $dbName"
    Write-Host "Local data volumes retained: $dbVolume ; $workVolume"
} finally {
    foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $old[$key]) }
}
