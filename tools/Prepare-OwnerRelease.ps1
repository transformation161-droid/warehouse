# Owner-local copy preparation only. Never give this script a working/live database.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [switch]$ConfirmedClosedBackup,
    [string]$Image='ksu-production:rc'
)
$ErrorActionPreference='Stop'
if (-not $ConfirmedClosedBackup) { throw 'A verified, independent, closed backup is required. Do not use a live database.' }
$inputFile=Get-Item -LiteralPath $Source
if ($inputFile.PSIsContainer -or $inputFile.Extension -ne '.sqlite3' -or ($inputFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Regular standalone SQLite backup required.' }
foreach ($suffix in @('-wal','-shm','-journal')) { if (Test-Path -LiteralPath ($inputFile.FullName+$suffix)) { throw 'Live/journalled input forbidden.' } }
$target=[IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path -LiteralPath $target) -or $target.StartsWith($inputFile.FullName.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'New private output directory required.' }
foreach ($path in @($inputFile.DirectoryName,(Split-Path -Parent $target))) {
    $ancestor=$path
    while ($ancestor) {
        if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Corporate backups, reports and prepared databases must stay outside Git.' }
        $next=Split-Path -Parent $ancestor;if ($next -eq $ancestor) { break };$ancestor=$next
    }
}
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Preparation failed. Source and existing backups were not replaced; retain the new private output/volume for inspection.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop/Linux required.' }
$imageId=Invoke-Docker image inspect --format '{{.Id}}' $Image
if ($imageId -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Preloaded verified release image required; no automatic pull.' }
$sourceHash=(Get-FileHash -Algorithm SHA256 -LiteralPath $inputFile.FullName).Hash
New-Item -ItemType Directory -Path $target | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $target -AclObject $acl
$name='ksu-owner-prepare-'+[Guid]::NewGuid().ToString('N');$volume=$name+'-data'
Invoke-Docker volume create $volume | Out-Null
$mount=@('--mount',"type=volume,src=$volume,dst=/output")
Invoke-Docker run --rm --pull never --network none --user 0:0 @mount --entrypoint python $imageId -c "import os; from pathlib import Path; assert not any(Path('/output').iterdir()); os.chown('/output',10001,10001); os.chmod('/output',0o700)"
Invoke-Docker run --pull never --name $name --network none --cpus 0.5 --memory 1g --memory-swap 1g --read-only --tmpfs '/tmp:rw,nosuid,nodev,size=32m' `
    -e KSU_SCHEMA_MODE=explicit-copy-migration @mount --mount "type=bind,src=$($inputFile.FullName),dst=/input/source,readonly" `
    --entrypoint python $imageId -m server.release_schema prepare-copy /input/source --closed-backup --destination /output/candidate
# An additional independent restore of the completed v4 copy, not just integrity_check.
Invoke-Docker run --rm --pull never --network none --cpus 0.5 --memory 1g --memory-swap 1g --read-only --tmpfs '/tmp:rw,nosuid,nodev,size=32m' `
    @mount --entrypoint python $imageId -c "from pathlib import Path; from server.backup_job import verify,atomic_json; root=Path('/output/candidate'); atomic_json(root/'restore-report.json',verify(root/'ksu.sqlite3','/output'),exclusive=True)"
foreach ($file in @('ksu.sqlite3','migration-report.json','restore-report.json')) { Invoke-Docker cp "${name}:/output/candidate/$file" (Join-Path $target $file) }
$migration=Get-Content -Raw -LiteralPath (Join-Path $target 'migration-report.json') | ConvertFrom-Json
$restore=Get-Content -Raw -LiteralPath (Join-Path $target 'restore-report.json') | ConvertFrom-Json
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $inputFile.FullName).Hash -ne $sourceHash -or $migration.source_sha256 -ne $sourceHash.ToLowerInvariant()) { throw 'Original backup checksum differs; do not deploy.' }
if (-not $migration.source_unchanged -or -not $migration.all_original_tables_preserved -or $migration.schema_after -ne 4 -or -not $restore.restored -or -not $restore.business_tables_identical -or (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $target 'ksu.sqlite3')).Hash.ToLowerInvariant() -ne $restore.sha256) { throw 'Migration/restore evidence mismatch; do not deploy.' }
Write-Host "Prepared schema v4 and independently restored: $target . Original backup unchanged; users/password hashes/media/history/receipts preserved."
Write-Host "Sessions were revoked only in the copy. Private container/volume retained: $name / $volume . No application, deployment or scheduled task was started."
