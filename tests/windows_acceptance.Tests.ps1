# Real native argv boundary; synthetic JSON/files only, no Docker daemon or private data.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('ksu-acceptance-argv-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$oldPath=$env:PATH
$oldRoot=$env:KSU_ACCEPTANCE_ARGV_ROOT
function Assert($condition,$message) { if (-not $condition) { throw $message } }
try {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
public class SyntheticDockerArgv {
    public static int Main(string[] args) {
        string root=Environment.GetEnvironmentVariable("KSU_ACCEPTANCE_ARGV_ROOT");
        string name="ksu-synthetic-acceptance-aaaaaaaaaaaa";
        File.AppendAllText(Path.Combine(root,"calls.txt"),String.Join("\t",args)+"\n");
        if(args.Length>0 && args[0]=="ps") { Console.WriteLine(name); return 0; }
        if(args.Length==3 && args[0]=="container" && args[1]=="inspect" && args[2]==name) {
            Console.Write(File.ReadAllText(Path.Combine(root,"inspect.json"))); return 0;
        }
        if(args.Length==2 && (args[0]=="stop" || args[0]=="rm") && args[1]==name) return 0;
        if(args.Length>0 && args[0]=="run" && Array.IndexOf(args,"--name")>=0) return 0;
        Console.Error.WriteLine("Unexpected native Docker arguments"); return 9;
    }
}
'@ -OutputAssembly (Join-Path $root 'docker.exe') -OutputType ConsoleApplication
    $env:KSU_ACCEPTANCE_ARGV_ROOT=$root;$env:PATH=$root+';'+$oldPath
    $name='ksu-synthetic-acceptance-aaaaaaaaaaaa';$id='a'*32
    $state=@{format='ksu-synthetic-acceptance-v1';syntheticOnly=$true;name=$name;volume=$name+'-data';id=$id;host='127.0.0.1';port=8017;image='sha256:synthetic';cert='synthetic-cert.pem';key='synthetic-key.pem';mode='pilot';viewerAccess='enabled'}
    $state | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $root 'state.json')
    function Write-Inspect($label,$containerName) {
        ConvertTo-Json -Depth 5 -InputObject @(@{Name=$containerName;Config=@{Labels=@{'ksu.synthetic.acceptance'=$label}}}) | Set-Content -Encoding UTF8 (Join-Path $root 'inspect.json')
    }
    Write-Inspect $id ('/'+$name)
    $script=Join-Path $PSScriptRoot '../tools/Set-Release-Acceptance.ps1'
    foreach ($mode in @('off','pilot','stop')) {
        & $script -Directory $root -Mode $mode
        $saved=Get-Content -Raw (Join-Path $root 'state.json') | ConvertFrom-Json
        Assert ($saved.mode -eq $mode -and $saved.id -eq $id -and $saved.volume -eq $state.volume) 'Mode switch changed identity or volume'
    }
    $calls=Get-Content (Join-Path $root 'calls.txt')
    Assert (($calls -match '^container\tinspect\tksu-synthetic-acceptance-aaaaaaaaaaaa$').Count -eq 3) 'JSON inspect did not cross the native argv boundary'
    Assert (($calls -match '^run\t').Count -eq 2) 'pilot/off did not recreate only the synthetic shell'
    Assert (-not ($calls -match 'volume\t(rm|prune)|--format.*Config.Labels')) 'Unsafe deletion or fragile template quoting'
    foreach ($scenario in @('foreign-label','foreign-name','invalid-json')) {
        $before=[IO.File]::ReadAllBytes((Join-Path $root 'state.json'))
        $count=(Get-Content (Join-Path $root 'calls.txt')).Count
        if ($scenario -eq 'foreign-label') { Write-Inspect 'foreign' ('/'+$name) }
        if ($scenario -eq 'foreign-name') { Write-Inspect $id '/foreign-container' }
        if ($scenario -eq 'invalid-json') { [IO.File]::WriteAllText((Join-Path $root 'inspect.json'),'{invalid') }
        $failed=$false
        try { & $script -Directory $root -Mode off } catch { $failed=$true }
        Assert $failed "Accepted $scenario"
        Assert ([Convert]::ToBase64String($before) -eq [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $root 'state.json')))) 'Rejected switch changed profile'
        $later=@(Get-Content (Join-Path $root 'calls.txt') | Select-Object -Skip $count)
        Assert (-not ($later -match '^(stop|rm|run)\t')) 'Rejected identity changed a container'
    }
    Write-Host 'PASS: native docker.exe JSON inspection, pilot/off/stop, foreign identity and malformed JSON refusal; data volume retained'
} finally {
    $env:PATH=$oldPath;$env:KSU_ACCEPTANCE_ARGV_ROOT=$oldRoot
    Remove-Item -Recurse -Force -LiteralPath $root
}
