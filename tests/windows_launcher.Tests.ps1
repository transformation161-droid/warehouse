# Portable contract tests: fake Docker, real launcher control flow and files; no daemon required.
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('ksu-launcher-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $root 'tools') -Force | Out-Null
Copy-Item (Join-Path $PSScriptRoot '../tools/Start-Postgres.ps1') (Join-Path $root 'tools/Start-Postgres.ps1')
$source = Join-Path $root 'check1.sqlite3'
[IO.File]::WriteAllText($source,'synthetic mock SQLite')
$global:ksuTestCalls = [Collections.Generic.List[string]]::new()
$global:ksuTestFailMigration = $false
$global:ksuTestMissingImage = $false
function global:docker {
    $line = $args -join ' '
    $global:ksuTestCalls.Add($line)
    $global:LASTEXITCODE=0
    if ($global:ksuTestMissingImage -and $args[0] -eq 'image') { $global:LASTEXITCODE=1; return }
    if ($args[0] -eq 'info') { return 'linux' }
    if ($line -match 'migrate-sqlite') {
        if ($global:ksuTestFailMigration) { $global:LASTEXITCODE=1; return }
        return '{"source_read_only":true,"sessions_revoked":true,"tables":{"users":{"count":2,"sha256":"mock"}}}'
    }
}
function global:Invoke-RestMethod { return @{status='ok'} }
function Assert($condition,$message) { if (-not $condition) { throw $message } }
try {
    $env:KSU_PG_RUNTIME_PASSWORD='sentinel'
    $env:KSU_PG_BACKUP_DIR='original-backup-dir'
    $global:ksuTestMissingImage=$true
    $failed=$false
    try { & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18082 } catch { $failed=$true }
    Assert $failed 'Missing image accepted'
    Assert (-not (Test-Path (Join-Path $root 'var/windows-postgres.json'))) 'Missing image allocated installation'
    $global:ksuTestMissingImage=$false
    & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18082
    Assert ($env:KSU_PG_RUNTIME_PASSWORD -eq 'sentinel') 'Environment not restored'
    $state = Get-Content -Raw (Join-Path $root 'var/windows-postgres.json') | ConvertFrom-Json
    Assert $state.ready 'Installation not ready'
    Assert ($env:KSU_PG_BACKUP_DIR -eq 'original-backup-dir') 'Backup directory environment not restored'
    Assert (($global:ksuTestCalls -match '-e KSU_PG_BACKUP_DSN -e KSU_PG_BACKUP_DIR').Count -eq 1) 'Backup configuration not passed to app'
    Assert (($global:ksuTestCalls -match "os.chown\('/work/backups',1000,1000\).*os.chmod\('/work/backups',0o700\)").Count -eq 1) 'Private persistent backup directory not initialized'
    Assert (($global:ksuTestCalls -match 'network create --opt com.docker.network.bridge.enable_icc=false').Count -eq 1) 'Missing isolated network'
    Assert (($global:ksuTestCalls -match 'migrate-sqlite').Count -eq 1) 'Expected exactly one migration'
    Assert (($global:ksuTestCalls -match 'dst=/input/source.sqlite3,readonly').Count -eq 2) 'Source must be read-only'
    Assert (($global:ksuTestCalls -match 'init --runtime-role ksu_runtime --backup-role ksu_backup').Count -eq 1) 'Missing restricted roles/schema'
    Assert (($global:ksuTestCalls -match '127.0.0.1:18082:8000').Count -eq 1) 'Port must be loopback only'
    $before = $global:ksuTestCalls.Count
    & (Join-Path $root 'tools/Start-Postgres.ps1') -Action Stop
    & (Join-Path $root 'tools/Start-Postgres.ps1')
    Assert (($global:ksuTestCalls -match 'migrate-sqlite').Count -eq 1) 'Restart migrated again'
    Assert (($global:ksuTestCalls -match '^(rm|volume rm) |password=sentinel').Count -eq 0) 'Unsafe Docker command'
    Remove-Item (Join-Path $root 'var/windows-postgres.json')
    $global:ksuTestFailMigration=$true
    $failed=$false
    try { & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18082 } catch { $failed=$true }
    Assert $failed 'Migration failure ignored'
    $before=$global:ksuTestCalls.Count
    $failed=$false
    try { & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18082 } catch { $failed=$true }
    Assert $failed 'Incomplete setup must refuse retries'
    Assert ($global:ksuTestCalls.Count -eq $before+1) 'Incomplete setup changed Docker state'
    Assert ($env:KSU_PG_RUNTIME_PASSWORD -eq 'sentinel') 'Failure leaked environment'
    Assert ($env:KSU_PG_BACKUP_DIR -eq 'original-backup-dir') 'Failure leaked backup directory environment'
    $originalState=Get-Content -Raw (Join-Path $root 'var/windows-postgres.json')
    $global:ksuTestFailMigration=$false
    & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18083 -Action RecoverNew -InstallationName windows-postgres-retry
    Assert ((Get-Content -Raw (Join-Path $root 'var/windows-postgres.json')) -eq $originalState) 'Recovery modified original state'
    Assert ((Get-Content -Raw (Join-Path $root 'var/windows-postgres-retry.json') | ConvertFrom-Json).ready) 'Recovery not ready'
    $before=$global:ksuTestCalls.Count
    $failed=$false
    try { & (Join-Path $root 'tools/Start-Postgres.ps1') -Source $source -Port 18083 -Action RecoverNew -InstallationName windows-postgres-retry } catch { $failed=$true }
    Assert $failed 'Recovery overwrote existing installation'
    Assert ($before -eq $global:ksuTestCalls.Count) 'Reused recovery touched Docker'
    Assert (($global:ksuTestCalls -match '^(rm|volume rm) ').Count -eq 0) 'Recovery deleted resources'
    Assert (($global:ksuTestCalls -match 'migrate-sqlite.+--standalone').Count -eq 3) 'Missing standalone migration flag'
    Write-Host 'PASS: initialize, read-only source, stop/restart, no remigration, failed migration retained.'
} finally {
    Remove-Item Function:\docker -ErrorAction SilentlyContinue
    Remove-Item Function:\Invoke-RestMethod -ErrorAction SilentlyContinue
    Remove-Item Env:\KSU_PG_RUNTIME_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\KSU_PG_BACKUP_DIR -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $root
}
