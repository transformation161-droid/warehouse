[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Directory,[Parameter(Mandatory=$true)][ValidateSet('pilot','off','stop')][string]$Mode,[ValidateSet('enabled','off')][string]$ViewerAccess)
$ErrorActionPreference='Stop'
$state=Get-Content -Raw -LiteralPath (Join-Path $Directory 'state.json') | ConvertFrom-Json
if ($state.format -ne 'ksu-synthetic-acceptance-v1' -or -not $state.syntheticOnly -or $state.name -notmatch '^ksu-synthetic-acceptance-[a-f0-9]{12}$' -or $state.volume -ne ($state.name+'-data') -or $state.port -eq 8003) { throw 'Synthetic-only profile required.' }
if ($ViewerAccess) { $state.viewerAccess=$ViewerAccess }
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Synthetic operation failed; do not touch any existing installation.' } }
$names=Invoke-Docker ps -a --filter "name=^/$($state.name)$" --format '{{.Names}}'
if ($names) {
    # Read JSON instead of a Go template: Windows PowerShell 5.1 strips the
    # inner quotes of --format when passing them to native docker.exe.
    $containers=@(Invoke-Docker container inspect $state.name | ConvertFrom-Json)
    if ($containers.Count -ne 1 -or $containers[0].Name -ne ('/'+$state.name)) { throw 'Unexpected acceptance container identity.' }
    $label=$containers[0].Config.Labels.'ksu.synthetic.acceptance'
    if ($label -ne $state.id) { throw 'Container does not belong to this acceptance profile.' }
    Invoke-Docker stop $state.name | Out-Null
    if ($Mode -ne 'stop') { Invoke-Docker rm $state.name | Out-Null } # Stateless own shell only; named data volume retained.
}
if ($Mode -ne 'stop') {
    Invoke-Docker run -d --pull never --name $state.name --label "ksu.synthetic.acceptance=$($state.id)" --read-only --cpus 0.5 --memory 1g --memory-swap 1g --tmpfs '/tmp:rw,nosuid,nodev,size=32m' --cap-drop ALL --security-opt no-new-privileges `
      -p "$($state.host):$($state.port):8000" --mount "type=volume,src=$($state.volume),dst=/data" --mount "type=bind,src=$($state.cert),dst=/tls/cert.pem,readonly" --mount "type=bind,src=$($state.key),dst=/tls/key.pem,readonly" `
      -e "KSU_VIEWER_ACCESS=$($state.viewerAccess)" -e "KSU_OFFLINE_MODE=$Mode" -e "KSU_OFFLINE_DEPLOYMENT_ID=$($state.id)" -e KSU_OFFLINE_USER_IDS=inspector -e KSU_DB=/data/ksu.sqlite3 -e "KSU_ORIGIN=https://$($state.host):$($state.port)" -e KSU_RELEASE_TLS_CERT=/tls/cert.pem -e KSU_RELEASE_TLS_KEY=/tls/key.pem -e KSU_BACKUP_EXPORT_DIR=/data/export-work $state.image | Out-Null
}
$state.mode=$Mode;$state | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $Directory 'state.json')
Write-Host "Synthetic acceptance mode: $Mode . Same origin, deployment ID and database; local queue is not cleared."
