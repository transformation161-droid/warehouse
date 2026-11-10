[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference='Stop';$OutputEncoding=New-Object Text.UTF8Encoding($false)
$config=Get-Content -Raw -LiteralPath (Join-Path $Directory 'settings.json') | ConvertFrom-Json
if ($config.format -ne 'ksu-owner-backup-v1' -or $config.volume -notmatch '^ksu-owner-backup-[a-f0-9]{32}$' -or $config.image -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Untrusted backup profile.' }
$statusFile=Join-Path $Directory 'owner-status.json'
$last=if (Test-Path -LiteralPath $statusFile) { Get-Content -Raw -LiteralPath $statusFile | ConvertFrom-Json } else { $null }
$name='ksu-backup-run-'+[Guid]::NewGuid().ToString('N')
try {
    $credential=Import-Clixml -LiteralPath (Join-Path $Directory 'credential.xml')
    $inputJson=@{username=$credential.UserName;password=$credential.GetNetworkCredential().Password} | ConvertTo-Json -Compress
    $dockerArgs=@('run','-i','--pull','never','--name',$name,'--read-only','--cpus','0.5','--memory','1g','--memory-swap','1g','--tmpfs','/tmp:rw,nosuid,nodev,size=32m','--mount',"type=volume,src=$($config.volume),dst=/backups")
    if ($config.ca) { $dockerArgs+=@('--mount',"type=bind,src=$($config.ca),dst=/trust/ca.pem,readonly") }
    $dockerArgs+=@('--entrypoint','python',$config.image,'-m','server.backup_job','--origin',$config.origin,'--directory','/backups')
    if ($config.ca) { $dockerArgs+=@('--ca','/trust/ca.pem') }
    # Secret exists only in memory and stdin; never in arguments, env, settings or logs.
    $raw=$inputJson | & docker @dockerArgs;$code=$LASTEXITCODE;$inputJson=$null;$credential=$null
    if ($code -ne 0) { throw 'Verified backup job failed. The previous verified backup is retained.' }
    $result=($raw -join "`n") | ConvertFrom-Json
    if ($result.file -notmatch '^backup-\d{8}T\d{6}-[a-f0-9]{32}\.sqlite3$' -or -not $result.restored) { throw 'Missing independent restore evidence.' }
    $target=Join-Path (Join-Path $Directory 'exports') $result.file
    if (Test-Path -LiteralPath $target) { throw 'Refusing to overwrite an existing backup.' }
    & docker cp "${name}:/backups/$($result.file)" $target;if ($LASTEXITCODE -ne 0) { throw 'Export copy failed.' }
    & docker cp "${name}:/backups/$($result.file.Replace('.sqlite3','.verification.json'))" ($target+'.verification.json');if ($LASTEXITCODE -ne 0) { throw 'Evidence copy failed.' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash -ne $result.sha256) { throw 'Windows export hash mismatch.' }
    @{lastAttempt='success';lastSuccess=$result} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $statusFile
    & docker rm $name | Out-Null # Own stateless completed job only; backup volume and exports remain.
    Write-Host 'Backup downloaded, SHA verified and independently restored. Existing backups retained.'
} catch {
    @{lastAttempt='failed';lastSuccess=if ($last) {$last.lastSuccess} else {$null};attemptAt=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $statusFile
    Write-Error 'Backup failed. Check private status and Task Scheduler; previous successful files remain unchanged.'
    exit 1
}
