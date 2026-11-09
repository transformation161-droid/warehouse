# Owner-local only. No connection to Amvera; takes a FULL frozen downloaded /data copy.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$FrozenDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [switch]$ConfirmedStoppedCopy,
    [string]$Image='ksu-offline:rc'
)
$ErrorActionPreference='Stop'
if (-not $ConfirmedStoppedCopy) { throw 'Confirm that ALL writers were stopped before the complete /data directory was downloaded. Never use a live main-file copy.' }
$source=(Get-Item -LiteralPath $FrozenDirectory).FullName
if (-not (Test-Path -LiteralPath (Join-Path $source 'ksu.sqlite3') -PathType Leaf)) { throw 'A full frozen directory with ksu.sqlite3 is required.' }
$target=[IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path -LiteralPath $target) -or $target.StartsWith($source.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Use a NEW private output directory outside the frozen source.' }
$ancestor=Split-Path -Parent $target
while ($ancestor) { if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Backups must never be written inside a Git repository.' };$next=Split-Path -Parent $ancestor;if ($next -eq $ancestor) { break };$ancestor=$next }
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Backup failed. Input and existing backups remain unchanged. Retain the private output and Docker resources for inspection.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop/Linux required.' }
Invoke-Docker image inspect $Image | Out-Null
New-Item -ItemType Directory -Path $target | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $target -AclObject $acl
$name='ksu-independent-backup-'+[Guid]::NewGuid().ToString('N')
$volume=$name+'-data'
Invoke-Docker volume create $volume | Out-Null
$mount=@('--mount',"type=volume,src=$volume,dst=/output")
Invoke-Docker run --rm --pull never --network none --user 0:0 @mount --entrypoint python $Image -c "import os; from pathlib import Path; assert not any(Path('/output').iterdir()), 'New empty volume required'; os.chown('/output',10001,10001); os.chmod('/output',0o700)"
# All SQLite operations occur on the volume's Linux filesystem, never on the NTFS input/output.
Invoke-Docker run --pull never --name $name --network none --cpus 0.5 --memory 1g --memory-swap 1g --read-only --tmpfs '/tmp:rw,nosuid,nodev,size=32m' @mount `
    --mount "type=bind,src=$source,dst=/input,readonly" --entrypoint python $Image -m server.release_backup --frozen-directory /input --destination /output/ksu-backup.sqlite3 --confirmed-stopped-copy
$backup=Join-Path $target 'ksu-backup.sqlite3';$report=$backup+'.verification.json'
if ((Test-Path -LiteralPath $backup) -or (Test-Path -LiteralPath $report)) { throw 'Output appeared during backup; refusing to overwrite.' }
Invoke-Docker cp "${name}:/output/ksu-backup.sqlite3" $backup
Invoke-Docker cp "${name}:/output/ksu-backup.sqlite3.verification.json" $report
$evidence=Get-Content -Raw -LiteralPath $report | ConvertFrom-Json
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $backup).Hash -ne $evidence.backup_sha256) { throw 'Export hash mismatch. Do not use this copy.' }
Invoke-Docker run --rm --pull never --network none @mount --entrypoint python $Image -m server.release_schema inspect /output/ksu-backup.sqlite3
Write-Host "Independent backup verified: $backup . Original frozen files were unchanged. Sessions are revoked only in this backup."
Write-Host "Private evidence: $report . Backup container/volume retained: $name / $volume . No automatic deletion."
