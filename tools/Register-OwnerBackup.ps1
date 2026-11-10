# Prepare owner-local Windows scheduler. Never run on Amvera or in CI against a real account.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Origin,[string]$Image='ksu-offline:rc',[string]$At='21:00',[string]$CaCertificate='', [switch]$PlanOnly)
$ErrorActionPreference='Stop'
$uri=[Uri]$Origin
if ($uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.Fragment -or $Origin.EndsWith('/')) { throw 'Exact approved HTTPS origin required.' }
$time=[DateTime]::Today.Add([TimeSpan]::ParseExact($At,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture))
$id=[Guid]::NewGuid().ToString('N');$directory=Join-Path $env:LOCALAPPDATA "KSU-Control\backup-jobs\$id"
$taskName='KSU-Verified-Backup-'+$id
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value) -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$powershell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
# Process-only policy. Never change CurrentUser/LocalMachine or override Group Policy.
$commonArguments='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File'
$action=New-ScheduledTaskAction -Execute $powershell -Argument "$commonArguments `"$(Join-Path $directory 'Invoke-OwnerBackup.ps1')`" -Directory `"$directory`""
$task=New-ScheduledTask -Action $action -Trigger (New-ScheduledTaskTrigger -Daily -At $time) -Principal $principal -Settings $settings
$healthAction=New-ScheduledTaskAction -Execute $powershell -Argument "$commonArguments `"$(Join-Path $directory 'Test-OwnerBackup.ps1')`" -Directory `"$directory`" -Notify"
$healthTask=New-ScheduledTask -Action $healthAction -Trigger (New-ScheduledTaskTrigger -Daily -At $time.AddMinutes(45)) -Principal $principal -Settings $settings
if ($PlanOnly) { return $task } # Pure task-definition test: no credentials/files/volumes/tasks created.
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Backup setup failed; existing resources are unchanged.' } }
$imageId=Invoke-Docker image inspect --format '{{.Id}}' $Image
if ($imageId -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Preloaded approved image required; no automatic pull.' }
$credential=Get-Credential -Message 'Personal manager account for the approved backup endpoint. No shared guest account.'
if (-not $credential) { throw 'Credential was not provided.' }
New-Item -ItemType Directory -Path $directory | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
Set-Acl -LiteralPath $directory -AclObject $acl
$credential | Export-Clixml -LiteralPath (Join-Path $directory 'credential.xml') # DPAPI, this Windows user only.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Invoke-OwnerBackup.ps1'),(Join-Path $PSScriptRoot 'Test-OwnerBackup.ps1') -Destination $directory
New-Item -ItemType Directory -Path (Join-Path $directory 'exports') | Out-Null
$volume='ksu-owner-backup-'+$id
Invoke-Docker volume create $volume | Out-Null
Invoke-Docker run --rm --pull never --network none --user 0:0 --mount "type=volume,src=$volume,dst=/backups" --entrypoint python $imageId -c "import os; from pathlib import Path; assert not any(Path('/backups').iterdir()); os.chown('/backups',10001,10001); os.chmod('/backups',0o700)"
$config=@{format='ksu-owner-backup-v1';origin=$Origin;image=$imageId;volume=$volume;ca='';task=$taskName}
if ($CaCertificate) { Copy-Item -LiteralPath $CaCertificate -Destination (Join-Path $directory 'ca.pem');$config.ca=Join-Path $directory 'ca.pem' }
$config | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $directory 'settings.json')
Register-ScheduledTask -TaskName $taskName -InputObject $task | Out-Null
Register-ScheduledTask -TaskName ($taskName+'-Health') -InputObject $healthTask | Out-Null
Write-Host "Registered personal backup and health tasks: $taskName . Profile: $directory"
Write-Host 'Run once and verify independently before accepting RPO. Windows, Docker and this user session must remain available. No old backups are deleted.'
