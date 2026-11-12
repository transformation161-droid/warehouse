# Native Windows PowerShell 5 and docker.exe argv; synthetic stub, no daemon/CA changes.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('pr41 native space '+[Guid]::NewGuid().ToString('N'))
$kit=$env:PR41_KIT
$oldPath=$env:PATH;$oldRoot=$env:PR41_NATIVE_ROOT;$testCA=$null;$consentJob=$null
function Assert($condition,$message) { if (-not $condition) { throw $message } }
function Refuses([scriptblock]$operation,$message) { $failed=$false;try { & $operation } catch {$failed=$true};Assert $failed $message }
New-Item -ItemType Directory -Path $root | Out-Null
try {
 $script=Join-Path $kit 'Start-PR41-Test.ps1'
 $tokens=$null;$errors=$null
 $null=[Management.Automation.Language.Parser]::ParseFile($script,[ref]$tokens,[ref]$errors)
 Assert ($errors.Count -eq 0) 'PowerShell parser errors'
 & $script -ValidateOnly -DataDirectory (Join-Path $root 'validation') -HostAddress 127.0.0.1
 Assert (-not (Test-Path (Join-Path $root 'validation'))) 'Validation created data'
 Refuses { & $script -ValidateOnly -DataDirectory (Join-Path $root 'unused') -HostAddress 8.8.8.8 } 'Accepted public bind'
 Refuses { & $script -ValidateOnly -DataDirectory (Join-Path $root 'unused') -Port 8019 } 'Accepted protected port'
 Refuses { & $script -ValidateOnly -DataDirectory $kit } 'Accepted package as private data'
 New-Item -ItemType Directory -Path (Join-Path $root 'foreign') | Out-Null
 'keep' | Set-Content (Join-Path $root 'foreign/data.txt')
 Refuses { & $script -ValidateOnly -DataDirectory (Join-Path $root 'foreign') } 'Accepted nonempty foreign folder'
 New-Item -ItemType Directory -Path (Join-Path $root 'git/.git') -Force | Out-Null
 Refuses { & $script -ValidateOnly -DataDirectory (Join-Path $root 'git/private') } 'Accepted private files in Git'
 'extra' | Set-Content (Join-Path $kit 'unlisted.txt')
 Refuses { & $script -ValidateOnly -DataDirectory (Join-Path $root 'unused') } 'Accepted extra package file'
 Remove-Item (Join-Path $kit 'unlisted.txt')
 Add-Type -ReferencedAssemblies System.Web.Extensions -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Web.Script.Serialization;
public class PR41SyntheticDocker {
 static string root=Environment.GetEnvironmentVariable("PR41_NATIVE_ROOT");
 static JavaScriptSerializer j=new JavaScriptSerializer();
 static string sha="cc0b18238fdca4cd02876b5daf0cbef4481c3eb2";
 static string image="sha256:"+new String('a',64);
 static Dictionary<string,object> D(params object[] items) { var d=new Dictionary<string,object>();for(int i=0;i<items.Length;i+=2)d[(string)items[i]]=items[i+1];return d; }
 static string FileFor(string kind,string name) { return Path.Combine(root,kind+"-"+name+".json"); }
 static void Save(string kind,string name,object data) { File.WriteAllText(FileFor(kind,name),j.Serialize(data)); }
 static Dictionary<string,object> Read(string kind,string name) { return j.Deserialize<Dictionary<string,object>>(File.ReadAllText(FileFor(kind,name))); }
 static string After(string[] args,string option) { int i=Array.IndexOf(args,option);return i<0?"":args[i+1]; }
 public static int Main(string[] a) {
  try {
   File.AppendAllText(Path.Combine(root,"calls.txt"),String.Join("\t",a)+"\n");
   if(a[0]=="info") { Console.WriteLine("linux");return 0; }
   if(a[0]=="image" && a[1]=="ls") { Console.WriteLine("instrakt-pr41-test:cc0b182");return 0; }
   if(a[0]=="image" && a[1]=="inspect") { Console.Write(j.Serialize(new object[]{D("Id",image,"Config",D("Labels",D("org.opencontainers.image.revision",sha)))}));return 0; }
   if(a[0]=="ps" || ((a[0]=="network" || a[0]=="volume") && a[1]=="ls")) {
    string kind=a[0]=="ps"?"container":a[0];
    foreach(string f in Directory.GetFiles(root,kind+"-*.json")) { var d=j.Deserialize<Dictionary<string,object>>(File.ReadAllText(f));Console.WriteLine(((string)d["Name"]).TrimStart('/')); }return 0;
   }
   if(a.Length==3 && a[1]=="inspect") { Console.Write("["+File.ReadAllText(FileFor(a[0],a[2]))+"]");return 0; }
   if((a[0]=="network" || a[0]=="volume") && a[1]=="create") {
    string n=a[a.Length-1];string label=After(a,"--label");Save(a[0],n,D("Name",n,"Labels",D("instrakt.pr41.test",label.Split('=')[1])));Console.WriteLine(n);return 0;
   }
   if(a[0]=="run") {
    string n=After(a,"--name");string label=After(a,"--label");
    if(label.Length==0) { Console.Error.WriteLine("Every helper must be labeled");return 8; }
    var d=D("Name","/"+n,"Image",image,"Config",D("Labels",D("instrakt.pr41.test",label.Split('=')[1])));
    if(Array.IndexOf(a,"prepare")>=0) { int x=Array.IndexOf(a,"prepare");d["host"]=a[x+3];d["port"]=a[x+4];d["instance"]=a[x+5]; }
    Save("container",n,d);Console.WriteLine(n);return 0;
   }
   if(a[0]=="cp") {
    string n=a[1].Split(':')[0];string dest=a[2];
    if(a[1].EndsWith("/private/.")) {
     var d=Read("container",n);string origin="https://"+d["host"]+":"+d["port"];
     var prepared=D("format","instrakt-pr41-test-v1","app_sha",sha,"instance",d["instance"],"origin",origin,"env_hashes",D(),"ca_thumbprint",new String('1',40));
     File.WriteAllText(Path.Combine(dest,"prepared.json"),j.Serialize(prepared));
     foreach(string f in new string[]{"server.env","bootstrap.env","runtime.env","test-accounts.txt","PR41-Test-CA.crt"})File.WriteAllText(Path.Combine(dest,f),"SYNTHETIC ONLY");
    } else if(a[1].EndsWith("/seed-complete.json")) File.WriteAllText(dest,"{\"seeded\":true}");else return 8;
    return 0;
   }
   if(a[0]=="exec") { if(Array.IndexOf(a,"pg_isready")>=0)return 0;if(Array.IndexOf(a,"python")>=0) { Console.WriteLine(sha);return 0; } }
   if(a[0]=="stop" || a[0]=="start") { Console.WriteLine(a[1]);return 0; }
   Console.Error.WriteLine("Unexpected synthetic native argv");return 9;
  } catch(Exception) { Console.Error.WriteLine("Synthetic Docker stub failed");return 10; }
 }
}
'@ -OutputAssembly (Join-Path $root 'docker.exe') -OutputType ConsoleApplication
 $env:PR41_NATIVE_ROOT=$root;$env:PATH=$root+';'+$oldPath
 $listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0);$listener.Start();$port=$listener.LocalEndpoint.Port;$listener.Stop()
 $profile=Join-Path $root 'private profile with spaces'
 & $script -DataDirectory $profile -HostAddress 127.0.0.1 -Port $port
 $state=Get-Content -Raw (Join-Path $profile 'state.json') | ConvertFrom-Json
 $before=(Get-FileHash (Join-Path $profile 'private/test-accounts.txt')).Hash
 & $script -Action Stop -DataDirectory $profile
 & $script -DataDirectory $profile
 & $script -Action Status -DataDirectory $profile
 Assert ((Get-FileHash (Join-Path $profile 'private/test-accounts.txt')).Hash -eq $before) 'Restart overwrote passwords'
 $calls=Get-Content (Join-Path $root 'calls.txt')
 Assert (@($calls | Where-Object {$_ -match '^run\t' -and $_ -match '\tseed\t/private$'}).Count -eq 1) 'Resumed profile seeded more than once'
 Assert (@($calls | Where-Object {$_ -match '^stop\t'}).Count -eq 2) 'Stop changed more than the owned app/database'
 Assert (-not ($calls -match 'volume\t(rm|prune)|\t0.0.0.0:|--format.*Config.Labels')) 'Unsafe deletion, bind or inspect template'
 Assert (@($calls | Where-Object {$_ -match '^run\t' -and $_ -match '--env-file\t[^\t]*private profile with spaces\\private\\runtime.env\t'}).Count -eq 1) 'Space-containing private path did not cross native argv intact'
 Assert (@($calls | Where-Object {$_ -match '^exec\t' -and $_ -match "\t-c\timport ssl,json,urllib.request;.*cafile='/tls/ca.crt'"}).Count -eq 2) 'Python probe lost native argument quotes'
 # The exact owner command including process-only ExecutionPolicy and real CA import.
 # This ephemeral CI user's certificate is removed by exact thumbprint in finally.
 Write-Host 'CA check: creating one ephemeral certificate in the CI user store.'
 $testSubject='INSTRAKT PR41 CI '+$state.instance
 $testCA=New-SelfSignedCertificate -Type Custom -Subject ('CN='+$testSubject) -CertStoreLocation Cert:\CurrentUser\My -KeyUsage CertSign,CRLSign -TextExtension @('2.5.29.19={critical}{text}ca=1') -NotAfter (Get-Date).AddDays(1) -KeyExportPolicy NonExportable
 Export-Certificate -Cert $testCA -FilePath (Join-Path $profile 'private/PR41-Test-CA.cer') | Out-Null
 $preparedPath=Join-Path $profile 'private/prepared.json'
 $prepared=Get-Content -Raw $preparedPath | ConvertFrom-Json
 Write-Host 'CA check: mismatch must refuse before changing the root store.'
 Refuses { & $script -Action Status -DataDirectory $profile -TrustTestCA } 'Imported mismatched CA'
 Assert (-not (Test-Path ('Cert:\CurrentUser\Root\'+$testCA.Thumbprint))) 'Rejected CA changed trust store'
 $prepared.ca_thumbprint=$testCA.Thumbprint;$prepared | ConvertTo-Json -Depth 5 | Set-Content $preparedPath
 Write-Host ('CA check: exact owner command via '+(Get-Command powershell.exe).Source)
 Write-Host ('CA check: native Docker stub via '+(Get-Command docker).Source)
 # Windows protects new current-user roots with a native consent dialog. The owner
 # clicks Yes; CI clicks only its own unique certificate in certutil's dialog.
 $consentJob=Start-Job -ArgumentList $testSubject -ScriptBlock {
  param($ExpectedSubject)
  Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
public class PR41OwnCertificateConsent {
 delegate bool Callback(IntPtr w,IntPtr p);
 [DllImport("user32.dll")] static extern bool EnumWindows(Callback c,IntPtr p);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr w,Callback c,IntPtr p);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr w,StringBuilder b,int n);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr w,StringBuilder b,int n);
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr w,out uint pid);
 [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr w,uint m,IntPtr a,IntPtr b);
 static string Text(IntPtr w) { var b=new StringBuilder(8192);GetWindowText(w,b,b.Capacity);return b.ToString(); }
 public static bool ApproveOnly(string subject) {
  bool clicked=false;
  EnumWindows(delegate(IntPtr w,IntPtr p) {
   uint pid;GetWindowThreadProcessId(w,out pid);
   try { if(Process.GetProcessById((int)pid).ProcessName!="certutil")return true; }catch { return true; }
   string text=Text(w);IntPtr yes=IntPtr.Zero;
   EnumChildWindows(w,delegate(IntPtr c,IntPtr q) {
    string t=Text(c);text+="\n"+t;var b=new StringBuilder(64);GetClassName(c,b,b.Capacity);
    if(b.ToString()=="Button" && t.Replace("&","")=="Yes")yes=c;return true;
   },IntPtr.Zero);
   if(yes!=IntPtr.Zero && text.Contains(subject)) { SendMessage(yes,0xF5,IntPtr.Zero,IntPtr.Zero);clicked=true;return false; }
   return true;
  },IntPtr.Zero);
  return clicked;
 }
}
'@
  for($i=0;$i -lt 300;$i++) {
   if([PR41OwnCertificateConsent]::ApproveOnly($ExpectedSubject)) { return 'own-test-CA-confirmed' }
   Start-Sleep -Milliseconds 100
  }
  throw 'The exact ephemeral test CA consent dialog was not found; no other dialog clicked.'
 }
 & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script -Action Status -DataDirectory $profile -TrustTestCA
 $consent=Receive-Job -Job $consentJob -Wait
 Assert ($consent -eq 'own-test-CA-confirmed') 'Native consent did not match this test CA'
 Remove-Job $consentJob;$consentJob=$null
 Assert ($LASTEXITCODE -eq 0 -and (Test-Path ('Cert:\CurrentUser\Root\'+$testCA.Thumbprint))) 'Process-only command/test CA import failed'
 $calls=Get-Content (Join-Path $root 'calls.txt')
 $meta=Join-Path $root ('container-'+$state.name+'-app.json')
 $object=Get-Content -Raw $meta | ConvertFrom-Json;$object.Config.Labels.'instrakt.pr41.test'='foreign';$object | ConvertTo-Json -Depth 8 | Set-Content $meta
 $count=$calls.Count
 Refuses { & $script -Action Stop -DataDirectory $profile } 'Stopped a container with a foreign label'
 $later=@(Get-Content (Join-Path $root 'calls.txt') | Select-Object -Skip $count)
 Assert (-not ($later -match '^(stop|run|start|rm)\t')) 'Rejected ownership changed a resource'
 Write-Host 'PASS: native PowerShell 5 start/stop/resume/status; spaces and JSON inspect; private paths/ports/ownership; no reseed/reset; exact test CA import and mismatch refusal.'
} finally {
 if ($consentJob) { Stop-Job $consentJob;Remove-Job $consentJob -Force }
 if ($testCA) { foreach ($store in @('Root','My')) { $certificate='Cert:\CurrentUser\'+$store+'\'+$testCA.Thumbprint;if (Test-Path $certificate) { Remove-Item $certificate -Force } } }
 $env:PATH=$oldPath;$env:PR41_NATIVE_ROOT=$oldRoot
 Remove-Item -Recurse -Force -LiteralPath $root
}
