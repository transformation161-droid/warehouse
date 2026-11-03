# Authenticated streaming download to a private local file; no Python or credential files.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][Uri]$Origin,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference='Stop'
if (-not $Origin.IsAbsoluteUri -or $Origin.UserInfo -or $Origin.Query -or $Origin.Fragment -or $Origin.AbsolutePath -ne '/') { throw 'Use the exact application origin, without a path or credentials.' }
if ($Origin.Scheme -ne 'https' -and -not $Origin.IsLoopback) { throw 'Remote download requires HTTPS with a trusted certificate.' }
$target=[IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw 'Destination exists; refusing overwrite.' }
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($target)))) { throw 'Choose an existing private folder, preferably on an encrypted disk.' }
$temp=$target+'.'+[Guid]::NewGuid().ToString('N')+'.partial'
Add-Type -AssemblyName System.Net.Http
$handler=[Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect=$false
$handler.CookieContainer=[Net.CookieContainer]::new()
$client=[Net.Http.HttpClient]::new($handler)
$client.Timeout=[TimeSpan]::FromMinutes(15)
$secure=$null
try {
    $username=Read-Host 'Manager login'
    $secure=Read-Host 'Password (not saved)' -AsSecureString
    $plain=([Net.NetworkCredential]::new('', $secure)).Password
    $body=@{username=$username;password=$plain} | ConvertTo-Json -Compress
    $content=[Net.Http.StringContent]::new($body,[Text.Encoding]::UTF8,'application/json')
    $client.DefaultRequestHeaders.Add('Origin',$Origin.GetLeftPart([UriPartial]::Authority))
    $login=$client.PostAsync(([Uri]::new($Origin,'api/login')),$content).GetAwaiter().GetResult()
    $plain=$null; $body=$null; $content.Dispose()
    if (-not $login.IsSuccessStatusCode) { throw 'Login failed.' }
    $account=$login.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
    if ($account.role -ne 'manager') { throw 'Only a manager can export a full backup.' }
    $client.DefaultRequestHeaders.Add('X-CSRF-Token',$account.csrf)
    $request=[Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post,[Uri]::new($Origin,'api/admin/backup'))
    $response=$client.SendAsync($request,[Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) { throw 'Backup export failed. Check the private server limits/configuration.' }
    $expected=[string]($response.Headers.GetValues('X-Backup-SHA256') | Select-Object -First 1)
    if ($expected -notmatch '^[a-f0-9]{64}$') { throw 'A verified PostgreSQL backup response is required.' }
    # Restrict the newly created file before writing any database bytes.
    $new=[IO.File]::Open($temp,[IO.FileMode]::CreateNew);$new.Dispose()
    $acl=Get-Acl -LiteralPath $temp
    $acl.SetAccessRuleProtection($true,$false)
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.AddAccessRule(([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','Allow')))
    $acl.AddAccessRule(([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-18'),'FullControl','Allow')))
    Set-Acl -LiteralPath $temp -AclObject $acl
    $inputStream=$response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $output=[IO.File]::Open($temp,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $inputStream.CopyToAsync($output,65536).GetAwaiter().GetResult();$output.Flush($true) } finally { $output.Dispose();$inputStream.Dispose();$response.Dispose();$request.Dispose() }
    $sha=[Security.Cryptography.SHA256]::Create();$read=[IO.File]::OpenRead($temp)
    try { $actual=([BitConverter]::ToString($sha.ComputeHash($read))).Replace('-','').ToLowerInvariant() } finally { $read.Dispose();$sha.Dispose() }
    if ($actual -ne $expected) { throw 'Checksum mismatch; no backup was published.' }
    [IO.File]::Move($temp,$target)
    Write-Host "Verified private backup saved: $target"
    Write-Host "SHA-256: $actual"
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    try { $client.PostAsync(([Uri]::new($Origin,'api/logout')),([Net.Http.StringContent]::new('{}'))).GetAwaiter().GetResult().Dispose() } catch { }
    if ($secure) { $secure.Dispose() }
    $plain=$null;$body=$null;$account=$null
    $client.Dispose();$handler.Dispose()
}
