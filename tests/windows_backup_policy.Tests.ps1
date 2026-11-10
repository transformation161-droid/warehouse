# Native Windows PowerShell 5.1 process test: actual generated actions, no registration or credentials.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('ksu synthetic policy '+[Guid]::NewGuid().ToString('N'))
$oldLocal=$env:LOCALAPPDATA
$before=Get-ExecutionPolicy -List | ConvertTo-Json -Compress
$global:KsuSyntheticTaskActions=New-Object System.Collections.Generic.List[object]
function Assert($condition,$message) { if (-not $condition) { throw $message } }
function New-ScheduledTaskAction {
    param($Execute,$Argument)
    $action=ScheduledTasks\New-ScheduledTaskAction -Execute $Execute -Argument $Argument
    $global:KsuSyntheticTaskActions.Add($action)
    return $action
}
try {
    foreach ($file in @('Register-OwnerBackup.ps1','Prepare-OwnerRelease.ps1','New-Offline-Backup.ps1')) {
        $tokens=$null;$errors=$null
        [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot "../tools/$file"),[ref]$tokens,[ref]$errors) | Out-Null
        Assert ($errors.Count -eq 0) "PowerShell syntax error: $file"
    }
    New-Item -ItemType Directory -Path $root | Out-Null
    $env:LOCALAPPDATA=$root
    $task=& (Join-Path $PSScriptRoot '../tools/Register-OwnerBackup.ps1') -Origin 'https://127.0.0.1:8030' -PlanOnly
    Assert ($global:KsuSyntheticTaskActions.Count -eq 2) 'Backup and health actions must both be defined before PlanOnly returns'
    Assert ($task.Settings.StartWhenAvailable -and $task.Settings.MultipleInstances -eq 2 -and $task.Principal.RunLevel -eq 0) 'Unsafe scheduling settings'
    foreach ($action in $global:KsuSyntheticTaskActions) {
        Assert ($action.Arguments -match '^-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "([^"]+)" -Directory "([^"]+)"( -Notify)?$') 'Missing process policy or invalid native quoting'
        $file=$Matches[1];$directory=$Matches[2]
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        @'
param([string]$Directory,[switch]$Notify)
if ((Get-ExecutionPolicy -Scope Process) -ne 'Bypass') { throw 'Wrong child process scope' }
[IO.File]::WriteAllText((Join-Path $Directory ((Split-Path -Leaf $PSCommandPath)+'.passed')),"Bypass;Notify=$Notify")
'@ | Set-Content -Encoding UTF8 -LiteralPath $file
        # Negative control proves script execution is blocked with Restricted.
        $blocked=Start-Process -FilePath $action.Execute -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Restricted -File "'+$file+'" -Directory "'+$directory+'"') -Wait -PassThru -WindowStyle Hidden
        Assert ($blocked.ExitCode -ne 0) 'Restricted control unexpectedly ran'
        $child=Start-Process -FilePath $action.Execute -ArgumentList $action.Arguments -Wait -PassThru -WindowStyle Hidden
        Assert ($child.ExitCode -eq 0 -and (Test-Path -LiteralPath ($file+'.passed'))) 'Generated task action did not run under process-only Bypass'
        if ($file.EndsWith('Test-OwnerBackup.ps1')) { Assert ((Get-Content -Raw ($file+'.passed')) -match 'Notify=True') 'Health notification argument lost' }
    }
    Assert ((Get-ExecutionPolicy -List | ConvertTo-Json -Compress) -eq $before) 'Parent or persistent execution policy changed'
    Write-Host 'PASS: both native scheduled actions run with process-only Bypass; Restricted control blocks; persistent policies unchanged'
} finally {
    Remove-Item function:New-ScheduledTaskAction -ErrorAction SilentlyContinue
    Remove-Variable -Scope Global -Name KsuSyntheticTaskActions -ErrorAction SilentlyContinue
    $env:LOCALAPPDATA=$oldLocal
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
