[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$CertificatePath,[Parameter(Mandatory=$true)][string]$KeyPath,
 [string]$HostAddress='127.0.0.1',[ValidateRange(1024,65535)][int]$Port=8017,
 [string]$Image='ksu-offline:rc',[string]$SeedImage='ksu-offline-acceptance:rc')
$ErrorActionPreference='Stop'
$ip=[Net.IPAddress]::Parse($HostAddress);$octets=$ip.GetAddressBytes()
if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or -not ($HostAddress -eq '127.0.0.1' -or $octets[0] -eq 10 -or ($octets[0] -eq 192 -and $octets[1] -eq 168) -or ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31)) -or $Port -eq 8003) { throw 'Use loopback or an approved private IPv4 address and a dedicated port, never8003.' }
function Invoke-Docker { & docker @args;if ($LASTEXITCODE -ne 0) { throw 'Synthetic acceptance failed; existing resources were not changed.' } }
$imageId=Invoke-Docker image inspect --format '{{.Id}}' $Image
$seedId=Invoke-Docker image inspect --format '{{.Id}}' $SeedImage
$id=[Guid]::NewGuid().ToString('N');$name='ksu-synthetic-acceptance-'+$id.Substring(0,12);$volume=$name+'-data'
$directory=Join-Path $env:LOCALAPPDATA "KSU-Control\synthetic-acceptance\$id"
New-Item -ItemType Directory -Path $directory | Out-Null
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetOwner($sid);$acl.SetAccessRuleProtection($true,$false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')));Set-Acl -LiteralPath $directory -AclObject $acl
$cert=(Get-Item -LiteralPath $CertificatePath).FullName;$key=(Get-Item -LiteralPath $KeyPath).FullName
Invoke-Docker volume create $volume | Out-Null
Invoke-Docker run --rm --pull never --network none --user 0:0 --mount "type=volume,src=$volume,dst=/data" --entrypoint python $imageId -c "import os; from pathlib import Path; assert not any(Path('/data').iterdir()); os.chown('/data',10001,10001); os.chmod('/data',0o700)"
Invoke-Docker run --pull never --name ($name+'-seed') --network none -e KSU_SCHEMA_MODE=explicit-copy-migration --mount "type=volume,src=$volume,dst=/data" $seedId | Out-Null
Invoke-Docker cp "$($name)-seed:/data/accounts.json" (Join-Path $directory 'accounts.json')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Set-Release-Acceptance.ps1') -Destination $directory
$state=@{format='ksu-synthetic-acceptance-v1';syntheticOnly=$true;id=$id;name=$name;volume=$volume;image=$imageId;cert=$cert;key=$key;host=$HostAddress;port=$Port;mode='pilot';viewerAccess='enabled'}
$state | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $directory 'state.json')
& (Join-Path $directory 'Set-Release-Acceptance.ps1') -Directory $directory -Mode pilot
Write-Host "SYNTHETIC ONLY: https://${HostAddress}:$Port . No corporate backup or existing database was mounted."
Write-Host "Private accounts/state: $directory . Credentials are not public or embedded in the app."
