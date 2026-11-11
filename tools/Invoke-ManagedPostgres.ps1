# Owner-local operations only; private env/CA/source files must never be in a Git checkout.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('bootstrap','template','probe','init-schema','init-production','migrate','verify','preflight','backup','restore')][string]$Operation,
    [Parameter(Mandatory=$true)][string]$EnvFile,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$Source='',
    [ValidateSet('runtime','admin','backup','bootstrap')][string]$Role='runtime',
    [string]$Image='ksu-managed:rc'
)
$ErrorActionPreference='Stop'
$configItem=Get-Item -LiteralPath $EnvFile
if ($configItem.PSIsContainer -or ($configItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Regular private env file required.' }
$config=$configItem.FullName;$configDirectory=Split-Path -Parent $config
$target=[IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $target) { throw 'New private output directory required; no overwrite.' }
$inputPath=''
if ($Source) { $item=Get-Item -LiteralPath $Source;if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Regular private input file required.' };$inputPath=$item.FullName }
if ($Operation -in @('bootstrap','init-production','migrate','verify','restore') -and -not $inputPath) { throw 'Explicit private input file required.' }
if ($Operation -eq 'migrate') {
    foreach ($suffix in @('-wal','-shm','-journal')) { if (Test-Path -LiteralPath ($inputPath+$suffix)) { throw 'Closed independent SQLite backup required.' } }
}
$privatePaths=@($configDirectory,(Split-Path -Parent $target))
if ($inputPath) { $privatePaths+=Split-Path -Parent $inputPath }
foreach ($path in $privatePaths) {
    $ancestor=$path
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points in private paths are forbidden.' }
        if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Secrets/data/reports must stay outside Git.' }
        $next=Split-Path -Parent $ancestor;if ($next -eq $ancestor) { break };$ancestor=$next
    }
}
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Managed PostgreSQL operation failed. Original input and existing backups were not replaced. Retain private output/resources for inspection.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop/Linux required.' }
$imageId=Invoke-Docker image inspect --format '{{.Id}}' $Image
if ($imageId -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Preloaded approved image required; no pull.' }
New-Item -ItemType Directory -Path $target | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $target -AclObject $acl
$name='ksu-managed-owner-'+[Guid]::NewGuid().ToString('N');$volume=$name+'-work'
Invoke-Docker volume create $volume | Out-Null
$work=@('--mount',"type=volume,src=$volume,dst=/work")
Invoke-Docker run --rm --pull never --network none --user 0:0 @work --entrypoint python $imageId -c "import os; from pathlib import Path; assert not any(Path('/work').iterdir()); Path('/work/result').mkdir(); os.chown('/work',10001,10001); os.chown('/work/result',10001,10001); os.chmod('/work',0o700)"
$operationArgs=@($Operation)
switch ($Operation) {
    bootstrap { }
    template { $operationArgs+=@('--output','/work/result/pilot-template.json') }
    init-schema { }
    init-production { $operationArgs+=@('--source','/input/source','--output','/work/result/production') }
    migrate { $operationArgs+=@('--source','/input/source','--output','/work/result/migration.json') }
    verify { $operationArgs+=@('--report','/input/source') }
    probe { $operationArgs+=@('--role',$Role) }
    preflight { $operationArgs+=@('--role',$Role) }
    backup { $operationArgs+=@('--output','/work/result/backup.ksubackup') }
    restore { $operationArgs+=@('--source','/input/source','--output','/work/result/restore.json') }
}
$inputMount=@();$before=''
if ($inputPath) { $inputMount=@('--mount',"type=bind,src=$inputPath,dst=/input/source,readonly");$before=(Get-FileHash -Algorithm SHA256 -LiteralPath $inputPath).Hash }
$arguments=@('run','--pull','never','--name',$name,'--cpus','0.5','--memory','1g','--memory-swap','1g','--read-only',
    '--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--cap-drop','ALL','--security-opt','no-new-privileges',
    '--env-file',$config,'-e','KSU_PG_MIGRATION_DIR=/work','-e','KSU_PG_RESTORE_DIR=/work','-e','TMPDIR=/work')
$arguments+=$work;$arguments+=$inputMount;$arguments+=@('--mount',"type=bind,src=$configDirectory,dst=/config,readonly",'--entrypoint','python',$imageId,'-m','server.managed_pg');$arguments+=$operationArgs
if ($Operation -eq 'bootstrap') {
    # Only generated service passwords in private JSON; no credentials in argv/env of this shell.
    $payload=Get-Content -Raw -LiteralPath $inputPath
    $arguments=@($arguments[0],'-i')+$arguments[1..($arguments.Count-1)]
    $payload | & docker @arguments
    $payload=$null
    if ($LASTEXITCODE -ne 0) { throw 'Role bootstrap refused. Inspect the new target privately.' }
} else { Invoke-Docker @arguments }
if ($inputPath -and (Get-FileHash -Algorithm SHA256 -LiteralPath $inputPath).Hash -ne $before) { throw 'Input checksum changed; do not activate.' }
Invoke-Docker cp "${name}:/work/result/." $target
Write-Host "Operation completed: $Operation . Private output: $target . Retained container/volume: $name / $volume"
Write-Host 'A successful dump is not an independently verified restore. Use Test-ManagedPostgresBackup.ps1; do not upload output to GitHub/Codex.'
