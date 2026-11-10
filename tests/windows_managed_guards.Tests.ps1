# Guard checks happen before any Docker call; synthetic private paths only.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('ksu managed guards '+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
function Refused($Call,$Pattern) {
    $failed=$false
    try { & $Call } catch { if ($_.Exception.Message -notmatch $Pattern) { throw };$failed=$true }
    if (-not $failed) { throw 'Unsafe owner operation was accepted' }
}
try {
    $envFile=Join-Path $root 'synthetic.env';Set-Content -LiteralPath $envFile -Value 'SYNTHETIC=only'
    $source=Join-Path $root 'synthetic.sqlite3';Set-Content -LiteralPath $source -Value 'SYNTHETIC ONLY'
    $output=Join-Path $root 'new-output'
    $tool=Join-Path $PSScriptRoot '../tools/Invoke-ManagedPostgres.ps1'
    Refused { & $tool -Operation backup -EnvFile $envFile -OutputDirectory $root } 'New private output'
    Refused { & $tool -Operation init-production -EnvFile $envFile -OutputDirectory $output } 'Explicit private input'
    Refused { & $tool -Operation migrate -EnvFile $envFile -OutputDirectory $output } 'Explicit private input'
    foreach ($operation in @('template','init-schema')) {
        # These operations require no Source: they must reach the Git-path guard.
        Refused { & $tool -Operation $operation -EnvFile (Join-Path $PSScriptRoot '../requirements.txt') -OutputDirectory $output } 'outside Git'
    }
    Set-Content -LiteralPath ($source+'-wal') -Value 'SYNTHETIC WAL'
    Refused { & $tool -Operation migrate -EnvFile $envFile -OutputDirectory $output -Source $source } 'Closed independent'
    Refused { & $tool -Operation preflight -EnvFile (Join-Path $PSScriptRoot '../requirements.txt') -OutputDirectory $output } 'outside Git'
    $restore=Join-Path $PSScriptRoot '../tools/Test-ManagedPostgresBackup.ps1'
    Refused { & $restore -Backup $source -OutputDirectory $output } 'Trusted .ksubackup'
    $junction=Join-Path $root 'junction';New-Item -ItemType Junction -Path $junction -Target $root | Out-Null
    try {
        Refused { & $tool -Operation backup -EnvFile $envFile -OutputDirectory (Join-Path $junction 'escaped-output') } 'Reparse points'
    } finally { [IO.Directory]::Delete($junction) }
    if (Test-Path -LiteralPath $output) { throw 'Rejected operation created output' }
    Write-Host 'PASS: owner guards reject live inputs, Git paths and overwrites before Docker/files/resources'
} finally { Remove-Item -LiteralPath $root -Recurse -Force }
