# Owner-local execution ONLY. This script must never be run on a corporate backup in Codex/GitHub.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][ValidateSet('sqlite','postgres')][string]$Backend,
    [Parameter(Mandatory=$true)][string[]]$UserIds,
    [Parameter(Mandatory=$true)][string]$CertificatePath,
    [Parameter(Mandatory=$true)][string]$KeyPath,
    [ValidateRange(1024,65535)][int]$Port=8016,
    [string]$Image='ksu-offline:rc'
)
$ErrorActionPreference='Stop'
if ($Port -eq 8003) { throw 'Port 8003 is reserved for the existing installation.' }
if ($UserIds.Count -eq 0 -or @($UserIds | Where-Object { $_ -notmatch '^[a-zA-Z0-9_-]{1,80}$' }).Count) { throw 'Use existing inspector IDs from the approved copy, never create demo users.' }
$sourceFile=Get-Item -LiteralPath $Source
if ($sourceFile.PSIsContainer) { throw 'Select a trusted independent backup file.' }
if ($Backend -eq 'sqlite') {
    if ($sourceFile.Extension -ne '.sqlite3') { throw 'SQLite requires a closed standalone .sqlite3 backup.' }
    foreach ($suffix in @('-wal','-shm','-journal')) { if (Test-Path -LiteralPath ($sourceFile.FullName+$suffix)) { throw 'Live/journalled SQLite files are forbidden.' } }
} elseif ($sourceFile.Extension -ne '.ksubackup') { throw 'PostgreSQL requires a trusted .ksubackup package.' }
$cert=(Get-Item -LiteralPath $CertificatePath).FullName;$key=(Get-Item -LiteralPath $KeyPath).FullName
function Invoke-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw 'Staging operation failed. Original backup and all created resources are retained; no automatic deletion.' } }
function New-Secret { $bytes=New-Object byte[] 32;$rng=[Security.Cryptography.RandomNumberGenerator]::Create();try {$rng.GetBytes($bytes);return [Convert]::ToBase64String($bytes)}finally{$rng.Dispose()} }
function Write-PrivateEnv($Path,$Values) { [IO.File]::WriteAllLines($Path,@($Values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }),(New-Object Text.UTF8Encoding($false))) }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop/Linux required.' }
Invoke-Docker image inspect $Image | Out-Null
$id=[Guid]::NewGuid().ToString('N');$name='ksu-release-stage-'+$id.Substring(0,12)
$base=Join-Path $env:LOCALAPPDATA 'KSU-Control\private-staging'
$directory=Join-Path $base $id
New-Item -ItemType Directory -Path $directory | Out-Null
# New private directory only: never alter ACLs of an existing backup or production folder.
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule);Set-Acl -LiteralPath $directory -AclObject $acl
$secretsDirectory=Join-Path $directory 'secrets'
New-Item -ItemType Directory -Path $secretsDirectory | Out-Null
$sourceHash=(Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash
$origin="https://127.0.0.1:$Port"
$appEnv=@{KSU_OFFLINE_MODE='staging';KSU_OFFLINE_DEPLOYMENT_ID=$id;KSU_OFFLINE_USER_IDS=($UserIds -join ',');KSU_ORIGIN=$origin;KSU_WORK_DIR='/data/work';KSU_SCHEMA_MODE='verified';KSU_RELEASE_TLS_CERT='/tls/cert.pem';KSU_RELEASE_TLS_KEY='/tls/key.pem'}
# SQLite WAL/locking belongs on Docker's Linux filesystem, not an NTFS/SMB bind.
$appVolume=$name+'-application-data'
Invoke-Docker volume create $appVolume | Out-Null
$dataMount=@('--mount',"type=volume,src=$appVolume,dst=/data")
Invoke-Docker run --rm --pull never --network none --user 0:0 @dataMount --entrypoint python $Image -c "import os; from pathlib import Path; assert not any(Path('/data').iterdir()), 'New empty volume required'; os.chown('/data',10001,10001); os.chmod('/data',0o700)"
$inputMount=@('--mount',"type=bind,src=$($sourceFile.FullName),dst=/input/source,readonly")
$network=@();$published=@('-p',"127.0.0.1:${Port}:8000")
if ($Backend -eq 'sqlite') {
    Invoke-Docker run --rm --pull never --network none -e KSU_SCHEMA_MODE=explicit-copy-migration @dataMount @inputMount --entrypoint python $Image `
        -m server.release_schema prepare-copy /input/source --closed-backup --destination /data/candidate
    $appEnv.KSU_DB='/data/candidate/ksu.sqlite3'
} else {
    $pg='postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
    Invoke-Docker image inspect $pg | Out-Null
    $postgresPassword=New-Secret;$runtimePassword=New-Secret;$backupPassword=New-Secret
    $dbEnv=Join-Path $secretsDirectory 'postgres.env';Write-PrivateEnv $dbEnv @{POSTGRES_PASSWORD=$postgresPassword}
    $dbName=$name+'-db';$volume=$name+'-database'
    Invoke-Docker run -d --pull never --name $dbName --memory 1g --memory-swap 1g --cpus 0.5 --env-file $dbEnv @published -v "${volume}:/var/lib/postgresql/data" $pg | Out-Null
    $ready=$false
    for ($n=0;$n -lt 90;$n++) { & docker exec $dbName pg_isready -U postgres *> $null;if ($LASTEXITCODE -eq 0) {$ready=$true;break};Start-Sleep -Seconds 1 }
    if (-not $ready) { throw 'New staging database did not become ready; no production resource was touched.' }
    $network=@('--network',"container:$dbName");$published=@()
    $adminEnv=Join-Path $secretsDirectory 'admin.env'
    Write-PrivateEnv $adminEnv @{KSU_PG_ADMIN_DSN="host=127.0.0.1 dbname=postgres user=postgres password=$postgresPassword";KSU_PG_RUNTIME_PASSWORD=$runtimePassword;KSU_PG_BACKUP_PASSWORD=$backupPassword;KSU_PG_RESTORE_DIR='/data'}
    Invoke-Docker run --rm --pull never @network --env-file $adminEnv --entrypoint python $Image -m server.pg_admin provision-roles --runtime-role ksu_runtime --backup-role ksu_backup
    Invoke-Docker run --rm --pull never @network --env-file $adminEnv @dataMount @inputMount --entrypoint python $Image -m server.pg_admin restore /input/source --runtime-role ksu_runtime --backup-role ksu_backup
    $appEnv.KSU_DATABASE_URL="host=127.0.0.1 dbname=postgres user=ksu_runtime password=$runtimePassword"
    $appEnv.KSU_PG_BACKUP_DSN="host=127.0.0.1 dbname=postgres user=ksu_backup password=$backupPassword"
    $appEnv.KSU_PG_BACKUP_DIR='/data/backups'
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash -ne $sourceHash) { throw 'Original backup changed; staging must not start.' }
$appFile=Join-Path $secretsDirectory 'app.env';Write-PrivateEnv $appFile $appEnv
$metadata=@{format='ksu-private-staging-v1';backend=$Backend;deploymentId=$id;sourceSha256=$sourceHash;application=$name;applicationVolume=$appVolume;origin=$origin;ready=$false}
$state=Join-Path $directory 'stage.json';$metadata | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $state
Invoke-Docker run -d --pull never --name $name --read-only --memory 1g --memory-swap 1g --cpus 0.5 --tmpfs '/tmp:rw,nosuid,nodev,size=32m' `
    --cap-drop ALL --security-opt no-new-privileges --env-file $appFile @network @published @dataMount `
    --mount "type=bind,src=$cert,dst=/tls/cert.pem,readonly" --mount "type=bind,src=$key,dst=/tls/key.pem,readonly" $Image | Out-Null
if ($Backend -eq 'sqlite') { Invoke-Docker cp "${name}:/data/candidate/migration-report.json" (Join-Path $directory 'migration-report.json') }
Write-Host "Staging candidate started: $origin/offline-visit/ . Use only the existing accounts from the backup."
Write-Host "Private evidence/state: $state . ready=false until the owner completes the runbook and verified restore."
Write-Host "Stop only the staging app: docker stop $name . All data/volumes/backups remain retained."
