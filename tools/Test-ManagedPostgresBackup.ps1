# Independent restore on a NEW LOCAL Docker PostgreSQL. No source DB/admin credentials or cloud API.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Backup,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$Image='ksu-managed:rc'
)
$ErrorActionPreference='Stop'
$item=Get-Item -LiteralPath $Backup
if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Regular private backup file required.' }
$source=$item.FullName;$target=[IO.Path]::GetFullPath($OutputDirectory)
if ([IO.Path]::GetExtension($source) -ne '.ksubackup' -or (Test-Path -LiteralPath $target)) { throw 'Trusted .ksubackup and NEW private output required.' }
foreach ($path in @((Split-Path -Parent $source),(Split-Path -Parent $target))) {
    $ancestor=$path
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points in private paths are forbidden.' }
        if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Backups/recovery files must stay outside Git.' }
        $next=Split-Path -Parent $ancestor;if ($next -eq $ancestor) { break };$ancestor=$next
    }
}
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Independent local restore failed; no existing database or backup was replaced. Retain private resources for inspection.' } }
function New-Secret { $bytes=New-Object byte[] 32;$rng=[Security.Cryptography.RandomNumberGenerator]::Create();try { $rng.GetBytes($bytes);return [Convert]::ToBase64String($bytes) } finally { $rng.Dispose() } }
function Write-PrivateText($Path,$Value) { [IO.File]::WriteAllText($Path,$Value,(New-Object Text.UTF8Encoding($false))) }
$pg='postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop/Linux required.' }
Invoke-Docker image inspect $pg | Out-Null
$imageId=Invoke-Docker image inspect --format '{{.Id}}' $Image
if ($imageId -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Approved preloaded release image required.' }
New-Item -ItemType Directory -Path $target | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $target -AclObject $acl
$hash=(Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash
$id=[Guid]::NewGuid().ToString('N');$name='ksu-managed-restore-'+$id;$pki=$name+'-tls';$data=$name+'-data';$work=$name+'-work'
foreach ($volume in @($pki,$data,$work)) { Invoke-Docker volume create $volume | Out-Null }
Invoke-Docker run --rm --pull never --network none --user 0:0 --mount "type=volume,src=$pki,dst=/pki" --entrypoint bash $pg -c "umask 077; openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj '/CN=KSU private local restore' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' -out /pki/cert.pem -keyout /pki/key.pem >/dev/null 2>&1; chmod 644 /pki/cert.pem"
Invoke-Docker run --rm --pull never --network none --user 0:0 --mount "type=volume,src=$work,dst=/work" --entrypoint python $imageId -c "import os; from pathlib import Path; assert not any(Path('/work').iterdir()); os.chown('/work',10001,10001); os.chmod('/work',0o700)"
$super=New-Secret;$passwords=@{ksu_admin=(New-Secret);ksu_runtime=(New-Secret);ksu_backup=(New-Secret)}
$serverEnv=Join-Path $target 'local-server.env';$bootstrapEnv=Join-Path $target 'local-bootstrap.env';$adminEnv=Join-Path $target 'local-admin.env'
Write-PrivateText $serverEnv "POSTGRES_PASSWORD=$super`nPOSTGRES_DB=ksu_restore`n"
Write-PrivateText $bootstrapEnv "KSU_PG_TRANSPORT_PROFILE=verify-full`nKSU_PG_BOOTSTRAP_DSN=host=localhost port=5432 dbname=ksu_restore user=postgres password=$super sslmode=verify-full sslrootcert=/config/cert.pem`n"
Write-PrivateText $adminEnv "KSU_PG_TRANSPORT_PROFILE=verify-full`nKSU_PG_ADMIN_DSN=host=localhost port=5432 dbname=ksu_restore user=ksu_admin password=$($passwords.ksu_admin) sslmode=verify-full sslrootcert=/config/cert.pem`n"
Invoke-Docker run -d --pull never --name $name --cpus 0.5 --memory 1g --memory-swap 1g --env-file $serverEnv `
    --mount "type=volume,src=$data,dst=/var/lib/postgresql/data" --mount "type=volume,src=$pki,dst=/private-tls,readonly" $pg `
    bash -c 'cp /private-tls/* /tmp/ && chown postgres:postgres /tmp/key.pem /tmp/cert.pem && chmod 600 /tmp/key.pem && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/cert.pem -c ssl_key_file=/tmp/key.pem' | Out-Null
$ready=$false
for ($n=0;$n -lt 90;$n++) { & docker exec $name pg_isready -U postgres *> $null;if ($LASTEXITCODE -eq 0) { $ready=$true;break };Start-Sleep -Seconds 1 }
if (-not $ready) { throw 'New local restore server did not start; existing resources unchanged.' }
$common=@('run','--rm','--pull','never','--network',"container:$name",'--cpus','0.5','--memory','1g','--memory-swap','1g','--read-only',
    '--mount',"type=volume,src=$work,dst=/work",'--mount',"type=volume,src=$pki,dst=/config,readonly",'-e','KSU_PG_RESTORE_DIR=/work')
$payload=$passwords | ConvertTo-Json -Compress
$bootstrapArgs=$common+@('-i','--env-file',$bootstrapEnv,'--entrypoint','python',$imageId,'-m','server.managed_pg','bootstrap')
$payload | & docker @bootstrapArgs
$payload=$null;$super=$null;$passwords=$null
if ($LASTEXITCODE -ne 0) { throw 'Local role bootstrap refused; do not treat the backup as verified.' }
$restoreArgs=$common+@('--env-file',$adminEnv,'--mount',"type=bind,src=$source,dst=/input/backup.ksubackup,readonly",'--entrypoint','python',$imageId,
    '-m','server.managed_pg','restore','--source','/input/backup.ksubackup','--output','/work/restore.json')
Invoke-Docker @restoreArgs
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash -ne $hash) { throw 'Original backup changed; verification rejected.' }
Invoke-Docker cp "${name}:/var/lib/postgresql/data/PG_VERSION" (Join-Path $target 'PG_VERSION')
# Copy private proof without starting an application or publishing any port.
$export=$name+'-proof'
Invoke-Docker create --name $export --mount "type=volume,src=$work,dst=/work" --entrypoint python $imageId -c 'pass' | Out-Null
Invoke-Docker cp "${export}:/work/restore.json" (Join-Path $target 'restore.json')
Invoke-Docker rm $export | Out-Null
Invoke-Docker stop $name | Out-Null
Write-Host "Independent restore PASS (all tables, revoked sessions, roles). Source SHA256 unchanged. Private report: $target"
Write-Host "Only the NEW local server was stopped: $name . Retained volumes: $data / $work / $pki . No ports, source credentials, paid resources or production changes."
