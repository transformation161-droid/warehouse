[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Directory,[switch]$Notify,[int]$MaxHours=26)
$ErrorActionPreference='Stop';$good=$false
try {
    $status=Get-Content -Raw -LiteralPath (Join-Path $Directory 'owner-status.json') | ConvertFrom-Json
    $last=$status.lastSuccess
    if ($status.lastAttempt -eq 'success' -and $last.restored -and $last.file -match '^backup-\d{8}T\d{6}-[a-f0-9]{32}\.sqlite3$') {
        $age=([DateTimeOffset]::UtcNow-[DateTimeOffset]::Parse($last.verifiedAt)).TotalHours
        $good=($age -ge 0 -and $age -le $MaxHours -and (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path (Join-Path $Directory 'exports') $last.file)).Hash -eq $last.sha256)
    }
} catch { $good=$false }
if (-not $good) {
    if ($Notify) { try { $shell=New-Object -ComObject WScript.Shell;$shell.Popup('Нет свежей проверенной резервной копии. Проверьте задачу и место на диске.',10,'ИНСТРАКТ | Складской учет',48) | Out-Null } catch {} }
    Write-Error 'NO VERIFIED FRESH BACKUP. Do not accept RPO or enable release.';exit 1
}
Write-Host 'PASS: fresh independently restored backup and Windows SHA-256 match.'
