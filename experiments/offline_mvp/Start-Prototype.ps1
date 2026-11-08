# Synthetic-only standalone server; no application database or production volumes.
[CmdletBinding()]
param(
    [string]$BindAddress='127.0.0.1',
    [ValidateRange(1024,65535)][int]$Port=8010,
    [string]$CertificatePath,
    [string]$KeyPath,
    [ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$Build='stage0-v1',
    [string]$AppImage='ksu-postgres:offline'
)
$ErrorActionPreference='Stop'
$address=[Net.IPAddress]::Parse($BindAddress)
$bytes=$address.GetAddressBytes()
if ($bytes.Length -ne 4 -or -not ($bytes[0] -eq 127 -or $bytes[0] -eq 10 -or ($bytes[0] -eq 192 -and $bytes[1] -eq 168) -or ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31))) { throw 'Use a loopback or private LAN IPv4 address.' }
if ($bytes[0] -ne 127 -and (-not $CertificatePath -or -not $KeyPath)) { throw 'LAN testing requires a trusted HTTPS certificate and key.' }
if ([bool]$CertificatePath -ne [bool]$KeyPath) { throw 'Certificate and key must be specified together.' }
function Invoke-Docker { & docker @args; if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed. No existing container is removed automatically.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Start Docker Desktop with Linux containers.' }
Invoke-Docker image inspect $AppImage | Out-Null
$repo=Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$mounts=@('--mount',"type=bind,src=$PSScriptRoot,dst=/prototype,readonly",'--mount',"type=bind,src=$repo/styles.css,dst=/ksu.css,readonly")
$parameters=@('server.py','--host','0.0.0.0','--port',"$Port",'--style','/ksu.css','--allowed-host',$BindAddress,'--build',$Build)
if ($CertificatePath) {
    $cert=(Get-Item -LiteralPath $CertificatePath).FullName
    $key=(Get-Item -LiteralPath $KeyPath).FullName
    $mounts+=@('--mount',"type=bind,src=$cert,dst=/cert.pem,readonly",'--mount',"type=bind,src=$key,dst=/key.pem,readonly")
    $parameters+=@('--cert','/cert.pem','--key','/key.pem')
} elseif ($BindAddress -eq '127.0.0.1') {
    # Bind inside the container, while the Docker host exposes loopback only.
    $parameters+=@('--allow-container-http')
}
Write-Host 'Only the stage-0 folder and CSS are mounted. No database, DSN or photo upload API.'
Write-Host 'Stop this standalone server: docker stop ksu-offline-stage0'
& docker run --rm --pull never --name ksu-offline-stage0 --read-only --tmpfs '/tmp:rw,nosuid,nodev,size=16m' `
    --cap-drop ALL --security-opt no-new-privileges -p "${BindAddress}:${Port}:$Port" @mounts `
    -w /prototype --entrypoint python $AppImage @parameters

if ($LASTEXITCODE -notin @(0,130,143)) { throw "Prototype server stopped with Docker exit $LASTEXITCODE. Existing application resources were not touched." }
