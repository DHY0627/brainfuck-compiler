# verify_awib.ps1 - reproduce every claim in awib/AWIB-REPORT.md
#
#   pwsh -File D:\git\brainfuck-compiler\awib\verify_awib.ps1
#
# Chain under test:
#   bfc.exe (C++ seed)  ->  awib.exe (native Win64 built from BF source)
#   awib.exe            ->  C / Go / Java / Ruby / Rust / Tcl / i386-ELF
#   awib.exe < awib.b   ->  awib_gen.c -> gcc -> awib2.exe (self-hosting)
#
# Requires: bfc.exe, gcc/g++, python3. WSL (bash.exe) only for the ELF step.

$ErrorActionPreference = 'Continue'
$AwibDir = $PSScriptRoot
$ProjDir = Split-Path $AwibDir -Parent
$SrcDir  = Join-Path $AwibDir 'awib-master'
$Work    = Join-Path $AwibDir 'work'
$BfcExe  = Join-Path $ProjDir 'bfc.exe'
$AwibB   = Join-Path $AwibDir 'awib.b'
$AwibExe = Join-Path $AwibDir 'awib.exe'
New-Item -ItemType Directory -Force -Path $Work | Out-Null

$script:Results = @()
function Record([string]$Name, [bool]$Ok, [string]$Detail) {
    $script:Results += [pscustomobject]@{ Step = $Name; Ok = $Ok; Detail = $Detail }
    $tag = if ($Ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("  [{0}] {1,-34} {2}" -f $tag, $Name, $Detail)
}
function Need([string]$Exe) {
    $c = Get-Command $Exe -ErrorAction SilentlyContinue
    return [bool]$c
}

Write-Host "=== awib verification ==="
Write-Host "  project : $ProjDir"
Write-Host ""

# ---------------------------------------------------------------- 0. prereqs
if (-not (Test-Path $BfcExe)) { Write-Host "FATAL: bfc.exe not found at $BfcExe"; exit 2 }
foreach ($t in @('gcc', 'python')) {
    if (-not (Need $t)) { Write-Host "FATAL: '$t' not on PATH"; exit 2 }
}

# ------------------------------------------------- 1. fetch awib if missing
if (-not (Test-Path (Join-Path $SrcDir 'util\bfpp.py'))) {
    Write-Host '--- fetching awib ---'
    $zip = Join-Path $AwibDir 'awib-master.zip'
    try {
        Invoke-WebRequest -Uri 'https://codeload.github.com/matslina/awib/zip/refs/heads/master' -OutFile $zip -TimeoutSec 120
        Expand-Archive -Path $zip -DestinationPath $AwibDir -Force
        Record 'fetch awib' $true ("{0} bytes" -f (Get-Item $zip).Length)
    } catch {
        Record 'fetch awib' $false $_.Exception.Message
        Write-Host 'FATAL: cannot obtain awib sources'; exit 2
    }
} else {
    Record 'awib sources present' $true $SrcDir
}

# ---------------------------------------------------- 2. build awib.b (LF!)
# bfpp.py must be run with binary stdout: CR would break the C/bash
# backslash-newline continuations inside the polyglot.
$py = (Get-Command python).Source
# Helper is written to a file (never passed through `python -c`) so that no
# quoting layer can mangle it.
$mkPy = Join-Path $Work 'mkawib.py'
$pyScript = @'
import subprocess, sys
# The child must emit pure LF (sys.stdout.reconfigure(newline='\n')):
# on Windows python's default text-mode stdout turns every '\n' into CRLF,
# and a CR after a backslash breaks the C/bash line-continuations of the
# awib polyglot.  (BF itself ignores the CR, but the file should match the
# upstream/Linux artefact exactly.)
child = (
    "import sys, runpy; "
    r"sys.stdout.reconfigure(newline='\n'); "
    "sys.argv = ['bfpp.py', '--interpreter', '--format', 'formats/awib-0.3', '--width', '79', "
    "'awib-skeleton.b']; "
    "runpy.run_path('util/bfpp.py', run_name='__main__')"
)
r = subprocess.run([sys.executable, '-c', child], capture_output=True, cwd=sys.argv[1])
if r.returncode != 0:
    sys.stderr.write(r.stderr.decode('utf-8', 'replace'))
    sys.exit(1)
open(sys.argv[2], 'wb').write(r.stdout)
print(len(r.stdout))
'@
[System.IO.File]::WriteAllText($mkPy, $pyScript, (New-Object System.Text.UTF8Encoding($false)))
& $py $mkPy $SrcDir $AwibB | Out-Null
if (Test-Path $AwibB) {
    $bytes = [System.IO.File]::ReadAllBytes($AwibB)
    $cr = ($bytes | Where-Object { $_ -eq 13 }).Count
    $bf = @(43,45,60,62,91,93,44,46)
    $n = 0; foreach ($x in $bytes) { if ($bf -contains $x) { $n++ } }
    Record 'build awib.b' ($cr -eq 0) ("{0} bytes, {1} BF instrs, CR={2}" -f $bytes.Length, $n, $cr)
} else {
    Record 'build awib.b' $false 'bfpp.py produced nothing'; exit 2
}

# ------------------------------------------------- 3. compile with bfc.exe
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$out = & $BfcExe $AwibB -o $AwibExe -c 70000 2>&1
$sw.Stop()
$ok = (Test-Path $AwibExe)
Record 'bfc.exe compiles awib.b' $ok ("{0:N2}s, {1} bytes PE32+" -f $sw.Elapsed.TotalSeconds, (Get-Item $AwibExe).Length)

# ------------------------------------- 4. hello world through awib.exe
$hello = "@lang_c`n++++++[->++++++++++++<]>.----[--<+++>]<-.+++++++..+++.[--->+<]>-----.--`n-[-<+++>]<.---[--->++++<]>-.+++.------.--------.-[---<+>]<.[--->+<]>-.`n"
$helloB = Join-Path $Work 'hello_langc.b'
[System.IO.File]::WriteAllText($helloB, $hello, [System.Text.Encoding]::ASCII)
$helloC = Join-Path $Work 'hello.c'
cmd /c "`"$AwibExe`" < `"$helloB`" > `"$helloC`""
$helloExe = Join-Path $Work 'hello.exe'
gcc -O2 -o $helloExe $helloC 2>$null
$printed = (cmd /c "`"$helloExe`"")
Record 'hello world via awib.exe' ($printed -eq 'Hello World!') ("gcc+run -> '$printed'")

# ------------------------- 5. reference cross-check with util/bfint.c
gcc -O2 -o (Join-Path $Work 'bfint.exe') (Join-Path $SrcDir 'util\bfint.c') 2>$null
if (Test-Path (Join-Path $Work 'bfint.exe')) {
    $refC = Join-Path $Work 'hello_ref.c'
    cmd /c "`"$Work\bfint.exe`" `"$AwibB`" 0 < `"$helloB`" > `"$refC`""
    $a = [System.IO.File]::ReadAllText($refC).Replace("`r`n", "`n")
    $b = [System.IO.File]::ReadAllText($helloC).Replace("`r`n", "`n")
    Record 'matches reference bfint' ($a -ceq $b) ("{0} bytes" -f $b.Length)
} else {
    Record 'matches reference bfint' $false 'bfint.c did not build'
}

# ---------------------- 6. awib's own systest, byte-exact (6/6 expected)
Set-Content -Path (Join-Path $Work 'bininit.c') -Encoding ascii -Value @'
#include <stdio.h>
#include <io.h>
#include <fcntl.h>
__attribute__((constructor)) static void bininit(void){ _setmode(_fileno(stdout), _O_BINARY); }
'@
gcc -O2 -c -o (Join-Path $Work 'bininit.o') (Join-Path $Work 'bininit.c') 2>$null

$cases = @(
    @{ n = 'basicops';  inp = 'basicops.in' },
    @{ n = 'comments';  inp = $null },
    @{ n = 'empty';     inp = $null },
    @{ n = 'endoffile'; inp = $null },
    @{ n = 'factor';    inp = 'factor.in' },
    @{ n = 'memoryhog'; inp = $null }
)
$pass = 0
foreach ($c in $cases) {
    $n = $c.n
    $src = Join-Path $SrcDir "systest\$n.b"
    $cfile = Join-Path $Work "$n.c"
    cmd /c "`"$AwibExe`" < `"$src`" > `"$cfile`""
    $prog = Join-Path $Work "$n.r.exe"
    gcc -O2 -o $prog $cfile (Join-Path $Work 'bininit.o') 2>$null
    $got = Join-Path $Work "$n.got"
    if ($c.inp) { cmd /c "`"$prog`" < `"$SrcDir\systest\$($c.inp)`" > `"$got`"" }
    else        { cmd /c "`"$prog`" < NUL > `"$got`"" }
    $g = [System.IO.File]::ReadAllBytes($got)
    $e = [System.IO.File]::ReadAllBytes((Join-Path $SrcDir "systest\$n.out"))
    $same = ($g.Length -eq $e.Length) -and (@(Compare-Object $g $e -SyncWindow 0).Count -eq 0)
    if ($same) { $pass++ }
    Record "systest $n" $same ("{0} B vs expected {1} B" -f $g.Length, $e.Length)
}
Record 'systest total' ($pass -eq 6) "$pass/6 byte-exact"

# ------------------------------------ 7. self-hosting: awib compiles awib
$genC = Join-Path $Work 'awib_gen.c'
cmd /c "`"$AwibExe`" < `"$AwibB`" > `"$genC`""
$awib2 = Join-Path $Work 'awib2.exe'
$sw2 = [System.Diagnostics.Stopwatch]::StartNew()
gcc -O2 -o $awib2 $genC (Join-Path $Work 'bininit.o') 2>$null
$sw2.Stop()
$h1 = Join-Path $Work 'h1.c'; $h2 = Join-Path $Work 'h2.c'
cmd /c "`"$AwibExe`" < `"$helloB`" > `"$h1`""
cmd /c "`"$awib2`"   < `"$helloB`" > `"$h2`""
$a = [System.IO.File]::ReadAllBytes($h1); $b = [System.IO.File]::ReadAllBytes($h2)
$identical = ($a.Length -eq $b.Length) -and (@(Compare-Object $a $b -SyncWindow 0).Count -eq 0)
Record 'self-host stage1 == stage2' $identical ("awib_gen.c {0} B -> awib2.exe {1} B in {2:N1}s" -f (Get-Item $genC).Length, (Get-Item $awib2).Length, $sw2.Elapsed.TotalSeconds)

# ------------------------------------- 8. cross-compile to i386 Linux ELF
$elfB = Join-Path $Work 'hello_386.b'
[System.IO.File]::WriteAllText($elfB, "@386_linux`n" + $hello.Substring($hello.IndexOf("`n") + 1), [System.Text.Encoding]::ASCII)
$elf = Join-Path $Work 'hello_386'
cmd /c "`"$AwibExe`" < `"$elfB`" > `"$elf`""
$eb = [System.IO.File]::ReadAllBytes($elf)
$isElf = ($eb.Length -gt 4) -and $eb[0] -eq 0x7f -and $eb[1] -eq 0x45 -and $eb[2] -eq 0x4c -and $eb[3] -eq 0x46
Record 'awib emits i386 ELF' $isElf ("{0} bytes, magic 7f 45 4c 46" -f $eb.Length)

$other = @{}
foreach ($t in @('lang_go', 'lang_java', 'lang_ruby', 'lang_rust', 'lang_tcl')) {
    $tb = Join-Path $Work "hello_$t.b"
    [System.IO.File]::WriteAllText($tb, "@$t`n" + $hello.Substring($hello.IndexOf("`n") + 1), [System.Text.Encoding]::ASCII)
    $to = Join-Path $Work "hello_$t.out"
    cmd /c "`"$AwibExe`" < `"$tb`" > `"$to`""
    $other[$t] = (Get-Item $to).Length
}
Record 'other 5 text backends' (($other.Values | Where-Object { $_ -gt 0 }).Count -eq 5) (($other.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)B" }) -join ' ')

# ------------------------------------- 9. run the ELF under WSL (optional)
if (Need 'bash') {
    $wslPath = '/mnt/' + ($elf.Substring(0, 1).ToLower()) + ($elf.Substring(2) -replace '\\', '/')
    $r = cmd /c "bash -e -c `"chmod +x $wslPath && $wslPath`" 2>&1"
    Record 'ELF runs on Linux (WSL)' ($r -eq 'Hello World!') ("output: '$r'")
} else {
    Record 'ELF runs on Linux (WSL)' $true 'skipped (no bash.exe)'
}

# ----------------------------------------------------------------- summary
$failed = @($script:Results | Where-Object { -not $_.Ok })
Write-Host ''
Write-Host ("=== {0}/{1} checks passed ===" -f ($script:Results.Count - $failed.Count), $script:Results.Count)
if ($failed.Count) { $failed | ForEach-Object { Write-Host ("  FAILED: {0} - {1}" -f $_.Step, $_.Detail) }; exit 1 }
Write-Host ("  usable compiler: {0}" -f $AwibExe)
exit 0
