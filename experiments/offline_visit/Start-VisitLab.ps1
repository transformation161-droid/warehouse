[CmdletBinding()]
param(
    [string]$BindAddress='127.0.0.1',
    [ValidateRange(1024,65535)][int]$Port=8012,
    [string]$CertificatePath,
    [string]$KeyPath,
    [string]$AppImage='ksu-postgres:offline'
)
$ErrorActionPreference='Stop'
if ($Port -eq 8003) { throw 'Port 8003 is reserved; the existing installation must not be touched.' }
$ip=[Net.IPAddress]::Parse($BindAddress);$bytes=$ip.GetAddressBytes()
if ($bytes.Length -ne 4 -or -not ($bytes[0] -eq 127 -or $bytes[0] -eq 10 -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168) -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31))) { throw 'Use a private LAN or loopback IPv4.' }
if ([bool]$CertificatePath -ne [bool]$KeyPath) { throw 'Provide both certificate and key.' }
if ($bytes[0] -ne 127 -and -not $CertificatePath) { throw 'LAN needs a trusted HTTPS certificate.' }
function Invoke-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed; no automatic removal or replacement is performed.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Start Docker Desktop in Linux mode.' }
Invoke-Docker image inspect $AppImage | Out-Null
$repo=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$var=Join-Path $repo 'var'
if ((Test-Path -LiteralPath $var) -and @(Get-ChildItem -LiteralPath $var -File -Force).Count -gt 0) { throw 'Existing installation files detected in var. Extract this PR to a separate new folder; do not update the working source folder.' }
$data=Join-Path $repo 'var\offline-stage2-lab'
if (-not (Test-Path -LiteralPath $data)) { New-Item -ItemType Directory -Path $data | Out-Null }
if ((Get-ChildItem -LiteralPath $data -Force).Count -gt 0 -and -not (Test-Path -LiteralPath (Join-Path $data 'stage2-lab.json'))) { throw 'Nonempty unmarked lab directory. Use a new source folder; nothing was deleted.' }
$mounts=@('--mount',"type=bind,src=$repo/server,dst=/app/server,readonly",'--mount',"type=bind,src=$PSScriptRoot,dst=/app/experiments/offline_visit,readonly",'--mount',"type=bind,src=$data,dst=/lab")
foreach ($file in @('styles.css','index.html','app.js','sync.js','decisions.js','calendar.js','writeoff_analytics.js')) {
    $path=Join-Path $repo $file
    $mounts+=@('--mount',"type=bind,src=$path,dst=/app/$file,readonly")
}
$scheme='http';$parameters=@('-m','experiments.offline_visit.lab','--directory','/lab','--host','0.0.0.0','--port',"$Port")
if ($CertificatePath) {
    $cert=(Get-Item -LiteralPath $CertificatePath).FullName;$key=(Get-Item -LiteralPath $KeyPath).FullName
    $mounts+=@('--mount',"type=bind,src=$cert,dst=/cert.pem,readonly",'--mount',"type=bind,src=$key,dst=/key.pem,readonly")
    $parameters+=@('--cert','/cert.pem','--key','/key.pem');$scheme='https'
}
$origin="${scheme}://${BindAddress}:${Port}";$parameters+=@('--origin',$origin)
Write-Host "Synthetic SQLite lab: $origin/offline-visit/"
Write-Host "Generated test accounts: $data\demo-accounts.txt (open after first start)."
Write-Host 'No existing database, DSN or production volume is mounted. PostgreSQL is not started.'
Write-Host 'Stop only this lab: docker stop ksu-offline-visit-stage2'
& docker run --rm --pull never --name ksu-offline-visit-stage2 --read-only --cpus 0.5 --memory 1g --memory-swap 1g `
    --tmpfs '/tmp:rw,nosuid,nodev,size=32m' --cap-drop ALL --security-opt no-new-privileges `
    -p "${BindAddress}:${Port}:$Port" @mounts -w /app --entrypoint python $AppImage @parameters
if ($LASTEXITCODE -notin @(0,130,143)) { throw 'Lab exited with an error; saved files were not removed.' }
