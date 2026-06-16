$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:GBP_REPO_URL) { $env:GBP_REPO_URL } else { "https://github.com/HansenHomeAI/git-branch-pane.git" }
$Ref = if ($env:GBP_REF) { $env:GBP_REF } else { "main" }
$AppDir = if ($env:GBP_APP_DIR) { $env:GBP_APP_DIR } else { Join-Path $HOME ".local\share\git-branch-pane" }
$SourceDir = if ($env:GBP_SOURCE_DIR) { $env:GBP_SOURCE_DIR } else { Join-Path $AppDir "source" }
$BinDir = if ($env:GBP_BIN_DIR) { $env:GBP_BIN_DIR } else { Join-Path $HOME ".local\bin" }
$TargetRepo = if ($args.Count -gt 0) { $args[0] } elseif ($env:GBP_TARGET_REPO) { $env:GBP_TARGET_REPO } else { (Get-Location).Path }
$HostName = if ($env:GBP_HOST) { $env:GBP_HOST } else { "127.0.0.1" }
$Port = if ($env:GBP_PORT) { $env:GBP_PORT } else { "8765" }

function Require-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Invoke-Git {
    & git @args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Find-Python {
    $candidates = @(
        @{ Exe = "python3"; Args = @() },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "py"; Args = @("-3") }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) {
            continue
        }
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $candidate.Exe @($candidate.Args) -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" > $null 2> $null
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]$candidate
            }
        } catch {
        } finally {
            $ErrorActionPreference = $previousPreference
        }
    }
    throw "Missing required Python 3. Install Python 3.9+ from python.org, winget, or the Microsoft Store."
}

function Add-To-UserPath($PathToAdd) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $PathToAdd) {
        [Environment]::SetEnvironmentVariable("Path", (($parts + $PathToAdd) -join ";"), "User")
        $env:Path = "$PathToAdd;$env:Path"
        Write-Host "Added $PathToAdd to the user PATH."
    }
}

function Ps-Quote($Value) {
    return $Value.Replace("'", "''")
}

Require-Command git
$Python = Find-Python

New-Item -ItemType Directory -Force -Path $AppDir, $BinDir | Out-Null

if (Test-Path (Join-Path $SourceDir ".git")) {
    Invoke-Git -C $SourceDir remote set-url origin $RepoUrl
    Invoke-Git -C $SourceDir fetch --depth 1 origin $Ref
    Invoke-Git -C $SourceDir checkout -B $Ref "origin/$Ref"
} else {
    if (Test-Path $SourceDir) {
        Move-Item $SourceDir "$SourceDir.backup.$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    }
    Invoke-Git clone --depth 1 --branch $Ref $RepoUrl $SourceDir
}

$AppPy = Join-Path $AppDir "git_branch_pane.py"
$LauncherPy = Join-Path $AppDir "gbp_launcher.py"
Copy-Item (Join-Path $SourceDir "git_branch_pane.py") $AppPy -Force
Copy-Item (Join-Path $SourceDir "gbp_launcher.py") $LauncherPy -Force

$CmdPath = Join-Path $BinDir "gbp.cmd"
$PythonArgs = ($Python.Args | ForEach-Object { '"' + $_ + '"' }) -join " "
@"
@echo off
setlocal
set "GBP_APP_PY=$AppPy"
"$($Python.Exe)" $PythonArgs "$LauncherPy" %*
"@ | Set-Content -Encoding ASCII -Path $CmdPath

$PsPath = Join-Path $BinDir "gbp.ps1"
$PsArgs = ($Python.Args | ForEach-Object { "'" + (Ps-Quote $_) + "'" }) -join ", "
if ($PsArgs) {
    $PsArgsLine = "@($PsArgs)"
} else {
    $PsArgsLine = "@()"
}
@"
`$env:GBP_APP_PY = '$(Ps-Quote $AppPy)'
`$pythonArgs = $PsArgsLine
& '$(Ps-Quote $($Python.Exe))' @pythonArgs '$(Ps-Quote $LauncherPy)' @args
exit `$LASTEXITCODE
"@ | Set-Content -Encoding ASCII -Path $PsPath

Add-To-UserPath $BinDir

Write-Host "Installed: $CmdPath"
Write-Host "Run from any Git repo: gbp"
Write-Host "Status: gbp --status"

if ($env:GBP_NO_RUN -eq "1") {
    exit 0
}

Write-Host ""
Write-Host "Starting persistent Git Branch Pane for: $TargetRepo"
& $CmdPath $TargetRepo --host $HostName --port $Port
exit $LASTEXITCODE
