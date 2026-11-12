# Isolated synthetic PR41 lab; never accepts a database URL, backup or existing volume.
[CmdletBinding()]
param(
 [ValidateSet('Start','Stop','Status')][string]$Action='Start',
 [string]$DataDirectory=(Join-Path $env:LOCALAPPDATA 'INSTRAKT\PR41-Test'),
 [string]$HostAddress='auto',
 [ValidateRange(1024,65535)][int]$Port=8041,
 [switch]$TrustTestCA,
 [switch]$ValidateOnly
)
$ErrorActionPreference='Stop'
$AppSha='cc0b18238fdca4cd02876b5daf0cbef4481c3eb2'
$PackageRoot=$PSScriptRoot
if ($PackageRoot.Contains(',')) { throw 'Extract the test kit to a local path without commas.' }
$directory=[IO.Path]::GetFullPath($DataDirectory)
if ($directory.StartsWith('\\') -or $directory.Contains(',') -or $directory.TrimEnd('\').Equals($PackageRoot.TrimEnd('\'),[StringComparison]::OrdinalIgnoreCase) -or $directory.StartsWith($PackageRoot.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Use a new local private data folder outside the package.' }
$ancestor=$directory
while ($ancestor) {
 if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points in data paths are forbidden.' }
 if (Test-Path -LiteralPath (Join-Path $ancestor '.git')) { throw 'Demo passwords/data must stay outside Git.' }
 $next=Split-Path -Parent $ancestor;if ($next -eq $ancestor) { break };$ancestor=$next
}
if ($Port -in @(8003,8018,8019)) { throw 'Protected existing port; use dedicated 8041.' }
function Assert-PrivateHost([string]$Value) {
 $parsed=$null
 if (-not [Net.IPAddress]::TryParse($Value,[ref]$parsed) -or $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Private IPv4 required.' }
 $b=$parsed.GetAddressBytes()
 if (-not ($Value -eq '127.0.0.1' -or $b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or ($b[0] -eq 192 -and $b[1] -eq 168))) { throw 'Only loopback or RFC1918 LAN addresses allowed; no public bind.' }
}
if ($HostAddress -ne 'auto') { Assert-PrivateHost $HostAddress }
function Check-Package {
 $file=Join-Path $PackageRoot 'package-manifest.json'
 if (-not (Test-Path -LiteralPath $file)) { throw 'Download and extract the complete PR41-Test package; do not run this script alone.' }
 $manifest=Get-Content -Raw -LiteralPath $file | ConvertFrom-Json
 if ($manifest.format -ne 'instrakt-pr41-test-v1' -or $manifest.app_sha -ne $AppSha) { throw 'Wrong PR41 package identity.' }
 foreach ($entry in $manifest.files.PSObject.Properties) {
  if ($entry.Name -match '(^/|\\|(^|/)\.\.(/|$))' -or $entry.Value -notmatch '^[a-f0-9]{64}$') { throw 'Invalid package path/hash.' }
  $path=Join-Path $PackageRoot $entry.Name
  $item=Get-Item -Force -LiteralPath $path
  if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Package hash mismatch; do not build/run.' }
 }
 $actual=@(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force -File | Where-Object { $_.FullName -ne $file })
 if ($actual.Count -ne @($manifest.files.PSObject.Properties).Count) { throw 'Unexpected extra files in source-only package.' }
}
Check-Package
$statePath=Join-Path $directory 'state.json'
$state=$null
if (Test-Path -LiteralPath $statePath) {
 $state=Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
 if ($state.format -ne 'instrakt-pr41-test-v1' -or $state.app_sha -ne $AppSha -or $state.instance -notmatch '^[a-f0-9]{32}$' -or $state.name -ne ('instrakt-pr41-'+$state.instance.Substring(0,12)) -or $state.port -in @(8003,8018,8019)) { throw 'Not an owned PR41 synthetic profile.' }
 Assert-PrivateHost $state.host
 if (($HostAddress -ne 'auto' -and $HostAddress -ne $state.host) -or ($PSBoundParameters.ContainsKey('Port') -and $Port -ne $state.port)) { throw 'Do not change an existing origin. Use a new DataDirectory for another LAN/port.' }
 if ($state.image -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Pinned image ID required.' }
} elseif ((Test-Path -LiteralPath $directory) -and @(Get-ChildItem -Force -LiteralPath $directory).Count) { throw 'Existing non-demo/nonempty folder refused. No overwrite or reset.' }
if ($ValidateOnly) { Write-Host 'PASS: pinned package and private path/endpoint guards; no Docker call or data change.';return }
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'PR41 test operation failed. Retain private files/resources; do not reset production or delete volumes.' } }
if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') { throw 'Docker Desktop must be running in Linux-container mode.' }
function Resource($kind,$name) {
 $existing=if ($kind -eq 'container') { @(Invoke-Docker ps -a --filter "name=^/${name}$" --format '{{.Names}}') } elseif ($kind -eq 'volume') { @(Invoke-Docker volume ls --filter "name=$name" --format '{{.Name}}') } else { @(Invoke-Docker network ls --filter "name=^${name}$" --format '{{.Name}}') }
 if ($existing -notcontains $name) { return $null }
 $objects=@(Invoke-Docker $kind inspect $name | ConvertFrom-Json)
 if ($objects.Count -ne 1) { throw 'Unexpected resource identity.' }
 $object=$objects[0]
 if (($kind -eq 'container' -and $object.Name -ne ('/'+$name)) -or ($kind -ne 'container' -and $object.Name -ne $name)) { throw 'Foreign resource name.' }
 $labels=if ($kind -eq 'container') {$object.Config.Labels} else {$object.Labels}
 if ($labels.'instrakt.pr41.test' -ne $state.instance) { throw 'Foreign resource label: no start/stop/change performed.' }
 if ($kind -eq 'container' -and $object.Image -ne $state.image) { throw 'Foreign container image.' }
 return $object
}
if (-not $state) {
 if ($Action -ne 'Start') { throw 'No test profile exists here.' }
 if ($HostAddress -eq 'auto') {
  $addresses=@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' -and $_.NetAdapter.HardwareInterface } | ForEach-Object { $_.IPv4Address.IPAddress } | Sort-Object -Unique)
  $private=@();foreach ($address in $addresses) { try { Assert-PrivateHost $address;$private+=$address } catch {} }
  if ($private.Count -eq 1) { $HostAddress=$private[0] } elseif ($private.Count -eq 0) { $HostAddress='127.0.0.1' } else { throw ('Several LAN addresses: '+($private -join ', ')+'. Rerun with -HostAddress for the Wi-Fi used by Android.') }
 }
 Assert-PrivateHost $HostAddress
 # Refuse an occupied test listener before creating any database or private state.
 $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse($HostAddress),$Port)
 try { $listener.Start() } finally { $listener.Stop() }
 $tag='instrakt-pr41-test:cc0b182'
 $imageNames=@(Invoke-Docker image ls --format '{{.Repository}}:{{.Tag}}')
 if ($imageNames -notcontains $tag) { Write-Host 'Building pinned PR41 application (first run only)...';Invoke-Docker build -t $tag -f (Join-Path $PackageRoot 'app-source/Dockerfile.managed-postgres') (Join-Path $PackageRoot 'app-source') | Out-Host }
 $images=@(Invoke-Docker image inspect $tag | ConvertFrom-Json)
 if ($images.Count -ne 1 -or $images[0].Id -notmatch '^sha256:[a-f0-9]{64}$' -or $images[0].Config.Labels.'org.opencontainers.image.revision' -ne $AppSha) { throw 'Image does not match approved PR41.' }
 $id=[Guid]::NewGuid().ToString('N')
 New-Item -ItemType Directory -Path $directory -Force | Out-Null
 $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
 $acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
 $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')));Set-Acl -LiteralPath $directory -AclObject $acl
 $state=[PSCustomObject]@{format='instrakt-pr41-test-v1';app_sha=$AppSha;instance=$id;name=('instrakt-pr41-'+$id.Substring(0,12));image=$images[0].Id;host=$HostAddress;port=$Port}
 $state | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $statePath
 New-Item -ItemType Directory -Path (Join-Path $directory 'private') | Out-Null
}
$name=$state.name;$network=$name+'-net';$data=$name+'-data';$pki=$name+'-tls';$privateVolume=$name+'-private';$pg=$name+'-db';$app=$name+'-app'
$label='instrakt.pr41.test='+$state.instance
# Validate ALL existing resources before touching any of them.
foreach ($item in @(@('network',$network),@('volume',$data),@('volume',$pki),@('volume',$privateVolume),@('container',($name+'-prepare')),@('container',($name+'-seed')),@('container',$pg),@('container',$app))) { $null=Resource $item[0] $item[1] }
$privateDirectory=Join-Path $directory 'private'
if ($Action -eq 'Stop') {
 foreach ($container in @($app,$pg)) { if (Resource 'container' $container) { Invoke-Docker stop $container | Out-Null } }
 Write-Host 'Only this synthetic app/database stopped. Test data, passwords and certificates retained.';return
}
if ($Action -eq 'Start') {
 if (-not (Resource 'network' $network)) { Invoke-Docker network create --label $label $network | Out-Null }
 foreach ($volume in @($data,$pki,$privateVolume)) { if (-not (Resource 'volume' $volume)) { Invoke-Docker volume create --label $label $volume | Out-Null } }
 $privateMount="type=volume,src=$privateVolume,dst=/private"
 $tlsMount="type=volume,src=$pki,dst=/tls"
 $helperMount="type=bind,src=$(Join-Path $PackageRoot 'demo.py'),dst=/acceptance/demo.py,readonly"
 $limits=@('--cpus','0.5','--memory','1g','--memory-swap','1g','--cap-drop','ALL','--security-opt','no-new-privileges')
 if (-not (Test-Path -LiteralPath (Join-Path $privateDirectory 'prepared.json'))) {
  if (@(Get-ChildItem -Force -LiteralPath $privateDirectory).Count) { throw 'Partial private setup retained. Inspect it; no automatic overwrite/retry.' }
  Invoke-Docker run --name ($name+'-prepare') --pull never --network none --user 0:0 --label $label --mount $privateMount --mount $tlsMount --mount $helperMount --entrypoint python $state.image /acceptance/demo.py prepare /private /tls $state.host $state.port $state.instance | Out-Host
  Invoke-Docker cp "$($name)-prepare:/private/." $privateDirectory
 }
 $prepared=Get-Content -Raw -LiteralPath (Join-Path $privateDirectory 'prepared.json') | ConvertFrom-Json
 foreach ($entry in $prepared.env_hashes.PSObject.Properties) { if ((Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $privateDirectory $entry.Name)).Hash.ToLowerInvariant() -ne $entry.Value) { throw 'Private test environment changed; no external database accepted.' } }
 if (-not (Resource 'container' $pg)) {
  # Database is NOT published on any host port. Only the new private Docker network.
  Invoke-Docker run -d --pull never --name $pg --label $label --network $network --network-alias pr41-db --user 0:0 --cpus 0.5 --memory 1g --memory-swap 1g --env-file (Join-Path $privateDirectory 'server.env') --mount "$privateMount,readonly" --mount "$tlsMount,readonly" --mount "type=volume,src=$data,dst=/var/lib/postgresql/data" --mount "type=bind,src=$(Join-Path $PackageRoot 'pg-start.sh'),dst=/acceptance/pg-start.sh,readonly" --entrypoint bash $state.image /acceptance/pg-start.sh | Out-Null
 } else { Invoke-Docker start $pg | Out-Null }
 $ready=$false;for ($n=0;$n -lt 120;$n++) { & docker exec $pg pg_isready -U postgres -d ksu_pr41_demo *> $null;if ($LASTEXITCODE -eq 0) {$ready=$true;break};Start-Sleep -Seconds 1 }
 if (-not $ready) { throw 'New test PostgreSQL not ready; existing resources untouched.' }
 if (-not (Test-Path -LiteralPath (Join-Path $privateDirectory 'seed-complete.json'))) {
  Invoke-Docker run --name ($name+'-seed') --label $label --pull never --network $network @limits --env-file (Join-Path $privateDirectory 'bootstrap.env') --mount $privateMount --mount "$tlsMount,readonly" --mount $helperMount --entrypoint python $state.image /acceptance/demo.py seed /private | Out-Host
  Invoke-Docker cp "$($name)-seed:/private/seed-complete.json" (Join-Path $privateDirectory 'seed-complete.json')
 }
 if (-not (Resource 'container' $app)) {
  Invoke-Docker run -d --pull never --name $app --label $label --network $network --read-only @limits --tmpfs '/tmp:rw,nosuid,nodev,size=768m' --env-file (Join-Path $privateDirectory 'runtime.env') --mount "$tlsMount,readonly" -p "$($state.host):$($state.port):8000" $state.image | Out-Null
 } else { Invoke-Docker start $app | Out-Null }
 $ready=$false
 $probe="import ssl,json,urllib.request; c=ssl.create_default_context(cafile='/tls/ca.crt'); r=urllib.request.Request('https://127.0.0.1:8000/api/health',headers={'Host':'$($state.host):$($state.port)'}); d=json.load(urllib.request.urlopen(r,context=c,timeout=3)); assert d['backend']=='postgresql'; print(d['build'])"
 for ($n=0;$n -lt 90;$n++) { $value=& docker exec $app python -c $probe 2>$null;if ($LASTEXITCODE -eq 0 -and $value -eq $AppSha) {$ready=$true;break};Start-Sleep -Seconds 1 }
 if (-not $ready) { throw 'Pinned app health/TLS check failed; retain the test resources.' }
}
$prepared=Get-Content -Raw -LiteralPath (Join-Path $privateDirectory 'prepared.json') | ConvertFrom-Json
if ($prepared.app_sha -ne $AppSha -or $prepared.instance -ne $state.instance -or $prepared.origin -ne ('https://'+$state.host+':'+$state.port)) { throw 'Prepared profile mismatch.' }
if ($TrustTestCA) {
 $caFile=Join-Path $privateDirectory 'PR41-Test-CA.cer'
 $publicCA=[Security.Cryptography.X509Certificates.X509Certificate2]::new($caFile)
 try { if ($publicCA.Thumbprint -ne $prepared.ca_thumbprint) { throw 'Test CA identity mismatch; trust store unchanged.' } } finally { $publicCA.Dispose() }
 $existing=Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $prepared.ca_thumbprint }
 if (-not $existing) { Import-Certificate -FilePath (Join-Path $privateDirectory 'PR41-Test-CA.cer') -CertStoreLocation Cert:\CurrentUser\Root -Confirm:$false | Out-Null }
}
Write-Host ('TEST ONLY URL: '+$prepared.origin)
Write-Host ('Application SHA: '+$AppSha+' ; PostgreSQL 17 synthetic; no external DB.')
Write-Host ('Test logins/passwords: '+(Join-Path $privateDirectory 'test-accounts.txt'))
Write-Host ('Public CA for Android USB transfer: '+(Join-Path $privateDirectory 'PR41-Test-CA.crt'))
Write-Host ('CA thumbprint: '+$prepared.ca_thumbprint+' ; leaf valid 7 days. No TLS bypass.')
if ($state.host -eq '127.0.0.1') { Write-Host 'Loopback only. Android needs a new profile with a private LAN HostAddress.' }
Write-Host ('To stop only this lab: run the same script with -Action Stop -DataDirectory "'+$directory+'"')
