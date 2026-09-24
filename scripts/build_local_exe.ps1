<#
.SYNOPSIS
    Build and run InkDoc as a real .exe from the current local working tree.

.DESCRIPTION
    Packages whatever is in the working tree right now (uncommitted changes
    included) with PyInstaller, using the same flags as
    .github/workflows/release.yml, then launches the resulting executable so it
    can be tested by hand.

    Output is kept out of the way of the CI-style directories:
      dist-local\   built bundle / exe
      build-local\  PyInstaller work dir, cache and generated spec

    The generated spec lives in build-local\, so the tracked inkdoc.spec is
    never overwritten. app/_version.py is stamped with a local dev version for
    the duration of the build and restored afterwards, so the working tree is
    left exactly as it was found.

.PARAMETER OneFile
    Build the single-file inkdoc.exe instead of the folder bundle. Slower to
    build and much slower to start (it unpacks ~120 MB on every launch), but it
    is the artifact users download, so use it when that is what you need to test.

.PARAMETER Clean
    Delete dist-local\ and build-local\ first and pass --clean to PyInstaller.
    Use after changing dependencies or when a build looks stale.

.PARAMETER BuildOnly
    Build, then stop without launching the app.

.PARAMETER RunOnly
    Skip the build and launch the executable from the previous run.

.PARAMETER Headless
    Launch with --headless (API server only, no window).

.PARAMETER NoWait
    Return to the prompt immediately instead of waiting for the app to close.

.PARAMETER NoVersionStamp
    Leave app/_version.py alone; the built exe reports the committed version.

.PARAMETER SkipChecks
    Skip the fast syntax / import preflight and go straight to packaging.

.PARAMETER Python
    Interpreter to build with. Defaults to the first python.exe on PATH, which
    must have requirements.txt and pyinstaller installed.

.PARAMETER AppArgs
    Extra arguments forwarded to the app, e.g. -AppArgs '--port','13500'.

.EXAMPLE
    .\scripts\build_local_exe.ps1
    Folder bundle from the current tree, then launch it.

.EXAMPLE
    .\scripts\build_local_exe.ps1 -OneFile -BuildOnly
    Produce the single-file dist-local\onefile\inkdoc.exe without launching.

.EXAMPLE
    .\scripts\build_local_exe.ps1 -RunOnly
    Relaunch the last build without rebuilding.
#>
[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$Clean,
    [switch]$BuildOnly,
    [switch]$RunOnly,
    [switch]$Headless,
    [switch]$NoWait,
    [switch]$NoVersionStamp,
    [switch]$SkipChecks,
    [string]$Python,
    [string[]]$AppArgs = @()
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$DistRoot = Join-Path $RepoRoot "dist-local"
$WorkRoot = Join-Path $RepoRoot "build-local"

if ($OneFile) {
    $ModeLabel = "onefile"
    $DistPath = Join-Path $DistRoot "onefile"
    $ExePath = Join-Path $DistPath "inkdoc.exe"
} else {
    $ModeLabel = "onedir"
    $DistPath = Join-Path $DistRoot "onedir"
    $ExePath = Join-Path $DistPath "inkdoc\inkdoc.exe"
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Note([string]$Message) {
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Stop-WithError([string]$Message) {
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Format-Size([long]$Bytes) {
    return ("{0:N1} MiB" -f ($Bytes / 1MB))
}

# Windows PowerShell 5.1 turns any native-command stderr write into a
# NativeCommandError when $ErrorActionPreference is Stop and the caller has
# merged streams (e.g. `build-local.bat > log.txt 2>&1`). Both python and
# PyInstaller write warnings to stderr routinely, so native calls go through
# these helpers, which drop the preference to Continue in their own scope and
# report success through the exit code instead.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    # Piped through Out-Host (not just left on the pipeline) so the native
    # command's own stdout can't merge into the function's return value
    # alongside $LASTEXITCODE — that silently turned every "success" exit
    # code into a truthy array once the command printed anything.
    & $Exe @Arguments | Out-Host
    return $LASTEXITCODE
}

function Invoke-NativeCapture {
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $stdout = & $Exe @Arguments
    $code = $LASTEXITCODE
    # $stdout is captured (not streamed) here, so it does not fall into the
    # return value the way it did in Invoke-Native above.
    # Force an array: Where-Object collapses a single match to a scalar
    # string, and string[-1] indexes its last character, not a missing
    # "last line" — that silently truncated git/version output to one char.
    $lines = @($stdout | Where-Object { $_ -and "$_".Trim() })
    if ($lines.Count -gt 0) { $text = "$($lines[$lines.Count - 1])".Trim() } else { $text = "" }
    return [pscustomobject]@{ Text = $text; ExitCode = $code }
}

function Resolve-BuildPython {
    if ($Python) {
        if (-not (Test-Path $Python)) { Stop-WithError "Interpreter not found: $Python" }
        return (Resolve-Path $Python).Path
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Stop-WithError "No 'python' on PATH. Install Python 3.10+ or pass -Python <path-to-python.exe>."
    }
    return $cmd.Source
}

function Get-TreeDescription {
    $sha = "nogit"
    $branch = "nogit"
    $dirty = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $head = Invoke-NativeCapture "git" @("-C", $RepoRoot, "rev-parse", "--short", "HEAD")
        if ($head.ExitCode -eq 0 -and $head.Text) { $sha = $head.Text }
        $name = Invoke-NativeCapture "git" @("-C", $RepoRoot, "rev-parse", "--abbrev-ref", "HEAD")
        if ($name.ExitCode -eq 0 -and $name.Text) { $branch = $name.Text }
        $status = Invoke-NativeCapture "git" @("-C", $RepoRoot, "status", "--porcelain")
        if ($status.ExitCode -eq 0 -and $status.Text) { $dirty = $true }
    } else {
        Write-Note "git unavailable; the build will not be stamped with a commit."
    }
    return [pscustomobject]@{ Sha = $sha; Branch = $branch; Dirty = $dirty }
}

Write-Host ""
Write-Host "InkDoc local build" -ForegroundColor White
Write-Host "==================" -ForegroundColor White

Set-Location $RepoRoot
$tree = Get-TreeDescription
if ($tree.Dirty) { $dirtyLabel = " (uncommitted changes)" } else { $dirtyLabel = " (clean)" }
Write-Note "repo   : $RepoRoot"
Write-Note "branch : $($tree.Branch) @ $($tree.Sha)$dirtyLabel"
Write-Note "mode   : $ModeLabel"

$BuiltVersion = "(unchanged from source)"
$Elapsed = $null

if ($RunOnly) {
    if (-not (Test-Path $ExePath)) {
        Stop-WithError "No previous $ModeLabel build at $ExePath. Run the script without -RunOnly first."
    }
    Write-Note "reusing the existing build (built $((Get-Item $ExePath).LastWriteTime))"
} else {
    $PythonExe = Resolve-BuildPython
    Write-Note "python : $PythonExe"

    if ((Invoke-Native $PythonExe @("-c", "import PyInstaller")) -ne 0) {
        Stop-WithError "PyInstaller is not installed for $PythonExe. Install it with:`n  `"$PythonExe`" -m pip install pyinstaller"
    }

    foreach ($required in @("main.py", "app\ui\index.html", "app\core\manifest.json", "assets\logo.ico")) {
        if (-not (Test-Path (Join-Path $RepoRoot $required))) {
            Stop-WithError "Required build input is missing: $required"
        }
    }

    if (-not $SkipChecks) {
        Write-Step "Preflight: syntax and dependencies"
        if ((Invoke-Native $PythonExe @("-m", "compileall", "-q", "main.py", "app")) -ne 0) {
            Stop-WithError "The working tree has syntax errors; build aborted."
        }
        if ((Invoke-Native $PythonExe @("-c", "import markitdown, webview, fastapi, uvicorn, magika")) -ne 0) {
            Stop-WithError "Runtime dependencies are missing for $PythonExe. Install them with:`n  `"$PythonExe`" -m pip install -r requirements.txt"
        }
        Write-Note "sources compile and core dependencies import"
    }

    if ($Clean) {
        Write-Step "Cleaning previous local build output"
        foreach ($stale in @($DistRoot, $WorkRoot)) {
            if (Test-Path $stale) {
                Remove-Item -Recurse -Force $stale
                Write-Note "removed $stale"
            }
        }
    }

    New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

    # app/_version.py is tracked, so the original content is restored in the
    # finally block below no matter how the build ends.
    $VersionFile = Join-Path $RepoRoot "app\_version.py"
    $VersionBackup = $null

    try {
        if (-not $NoVersionStamp) {
            Write-Step "Stamping a local build version"
            # Raw bytes, not Get-Content/Set-Content text round-tripping: Windows
            # PowerShell 5.1's -Encoding utf8 always adds a BOM, which would leave
            # the tracked file's on-disk bytes changed (BOM added) even after
            # "restoring" it to the same text.
            $VersionBackup = [System.IO.File]::ReadAllBytes($VersionFile)
            if ($tree.Dirty) { $shaLabel = "$($tree.Sha).dirty" } else { $shaLabel = $tree.Sha }
            $written = Invoke-Native $PythonExe @("scripts\write_version.py", "--dispatch", "--sha", $shaLabel)
            if ($written -ne 0) { Stop-WithError "Could not write app/_version.py." }
            # Read the stamped value back from the file rather than importing it:
            # some local interpreters (e.g. the embeddable distribution) don't put
            # the working directory on sys.path for `python -c`, so `import app`
            # fails there even though write_version.py already verified the write.
            $stampedMatch = [regex]::Match((Get-Content -Raw -Path $VersionFile), '__version__\s*=\s*"([^"]+)"')
            if ($stampedMatch.Success) { $BuiltVersion = $stampedMatch.Groups[1].Value }
        }

        Write-Step "Packaging with PyInstaller ($ModeLabel)"
        Write-Note "first build takes a few minutes; later builds reuse the build-local\ cache"

        # --specpath points PyInstaller at build-local\, and it resolves relative
        # --add-data sources against THAT directory, not the CWD -- so the data
        # sources have to be absolute here even though inkdoc.spec (built with no
        # --specpath override) can get away with relative ones.
        $uiSrc = Join-Path $RepoRoot "app\ui"
        $assetsSrc = Join-Path $RepoRoot "assets"
        $manifestSrc = Join-Path $RepoRoot "app\core\manifest.json"
        $iconSrc = Join-Path $RepoRoot "assets\logo.ico"

        # Kept deliberately in step with the PyInstaller invocation in
        # .github/workflows/release.yml, so a local .exe behaves like a shipped one.
        $pyiArgs = @(
            "-m", "PyInstaller",
            "--noconfirm",
            "--windowed",
            "--name", "inkdoc",
            "--icon", $iconSrc,
            "--distpath", $DistPath,
            "--workpath", $WorkRoot,
            "--specpath", $WorkRoot,
            "--log-level", "WARN",
            "--collect-all", "markitdown",
            "--collect-all", "magika",
            "--collect-all", "uvicorn",
            "--collect-all", "fastapi",
            "--collect-all", "webview",
            "--collect-all", "cryptography",
            "--collect-all", "pdfminer",
            "--collect-all", "pdfplumber",
            "--collect-all", "pypdfium2",
            "--add-data", "${uiSrc};app/ui",
            "--add-data", "${assetsSrc};assets",
            "--add-data", "${manifestSrc};app/core"
        )
        if ($OneFile) { $pyiArgs += "--onefile" } else { $pyiArgs += "--onedir" }
        if ($Clean) { $pyiArgs += "--clean" }
        $pyiArgs += "main.py"

        $started = Get-Date
        $buildCode = Invoke-Native $PythonExe $pyiArgs
        $Elapsed = (Get-Date) - $started
        if ($buildCode -ne 0) { Stop-WithError "PyInstaller build failed (exit $buildCode)." }
    }
    finally {
        if ($null -ne $VersionBackup) {
            [System.IO.File]::WriteAllBytes($VersionFile, $VersionBackup)
            Write-Note "restored app/_version.py"
        }
    }

    if (-not (Test-Path $ExePath)) { Stop-WithError "The build reported success but $ExePath is missing." }

    Write-Step "Build complete"
    Write-Note ("version : {0}" -f $BuiltVersion)
    Write-Note ("exe     : {0}" -f $ExePath)
    Write-Note ("size    : {0}" -f (Format-Size (Get-Item $ExePath).Length))
    if (-not $OneFile) {
        $bundleBytes = (Get-ChildItem -Recurse -File (Join-Path $DistPath "inkdoc") | Measure-Object -Property Length -Sum).Sum
        Write-Note ("bundle  : {0}" -f (Format-Size $bundleBytes))
    }
    Write-Note ("took    : {0:N0}s" -f $Elapsed.TotalSeconds)
}

if ($BuildOnly) {
    Write-Host ""
    Write-Host "Launch it with: $ExePath" -ForegroundColor Yellow
    exit 0
}

$launchArgs = @()
if ($Headless) { $launchArgs += "--headless" }
if ($AppArgs.Count -gt 0) { $launchArgs += $AppArgs }

Write-Step "Launching inkdoc.exe"
if ($launchArgs.Count -gt 0) { Write-Note ("args : {0}" -f ($launchArgs -join " ")) }
if ($OneFile) { Write-Note "onefile builds unpack on every start; the window can take ~10-30s to appear" }
Write-Note "converted .md files auto-save to $env:USERPROFILE\Downloads"

$startParams = @{
    FilePath         = $ExePath
    WorkingDirectory = (Split-Path -Parent $ExePath)
}
if ($launchArgs.Count -gt 0) { $startParams["ArgumentList"] = $launchArgs }

if ($NoWait) {
    Start-Process @startParams | Out-Null
    Write-Host ""
    Write-Host "Started in the background." -ForegroundColor Green
    exit 0
}

Write-Note "waiting for the app to close; close the window to return to the prompt"
$proc = Start-Process @startParams -PassThru
$proc.WaitForExit()
$code = $proc.ExitCode

Write-Host ""
if ($code -eq 0) {
    Write-Host "App closed normally." -ForegroundColor Green
} else {
    Write-Host "App exited with code $code." -ForegroundColor Yellow
}
exit $code
