$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# Refresh PATH so software installed by winget in the current Terminal session
# is visible without requiring the user to close and reopen Windows Terminal.
$MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($MachinePath -or $UserPath) {
    $env:Path = ($MachinePath + ';' + $UserPath).Trim(';')
}

function Test-PythonCandidate {
    param([string]$Exe, [string]$LauncherArg)
    try {
        if ($LauncherArg) {
            & $Exe $LauncherArg '-c' 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 1>$null 2>$null
        } else {
            & $Exe '-c' 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 1>$null 2>$null
        }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @(
        @{ Exe = 'py.exe'; Arg = '-3.14' },
        @{ Exe = 'py.exe'; Arg = '-3.13' },
        @{ Exe = 'py.exe'; Arg = '-3.12' },
        @{ Exe = 'python3.14.exe'; Arg = $null },
        @{ Exe = 'python3.13.exe'; Arg = $null },
        @{ Exe = 'python3.12.exe'; Arg = $null },
        @{ Exe = 'python.exe'; Arg = $null }
    )

    foreach ($candidate in $candidates) {
        if (Get-Command $candidate.Exe -ErrorAction SilentlyContinue) {
            if (Test-PythonCandidate $candidate.Exe $candidate.Arg) {
                return $candidate
            }
        }
    }
    return $null
}

function Invoke-Python {
    param(
        [hashtable]$Python,
        [string[]]$Arguments
    )
    if ($Python.Arg) {
        & $Python.Exe $Python.Arg @Arguments
    } else {
        & $Python.Exe @Arguments
    }
    return $LASTEXITCODE
}

$Python = Find-Python
if (-not $Python) {
    Write-Host 'Python 3.12 or newer is required.' -ForegroundColor Red
    Write-Host 'Install it with:'
    Write-Host '  winget install -e --id Python.Python.3.12'
    Write-Host 'Then run start.cmd again.'
    exit 1
}

if ($Python.Arg) {
    $VersionText = (& $Python.Exe $Python.Arg '--version' 2>&1 | Out-String).Trim()
} else {
    $VersionText = (& $Python.Exe '--version' 2>&1 | Out-String).Trim()
}
Write-Host "Using $VersionText"

$bootstrapCode = Invoke-Python -Python $Python -Arguments @((Join-Path $Root 'bootstrap_windows.py'))
if ($bootstrapCode -ne 0) { exit $bootstrapCode }

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Write-Host 'Node.js/npm is required.' -ForegroundColor Red
    Write-Host 'Install it with:'
    Write-Host '  winget install -e --id OpenJS.NodeJS.LTS'
    Write-Host 'Then run start.cmd again.'
    exit 1
}

$nodeVersion = (& node.exe '--version' 2>&1 | Out-String).Trim()
Write-Host "Using Node $nodeVersion"

if (Test-Path '.env') {
    foreach ($rawLine in Get-Content '.env') {
        $line = $rawLine.Trim()
        if (-not $line) { continue }
        if ($line.StartsWith('#')) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        $name = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if ($value.Length -ge 2) {
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

$env:LOCAL_COMPANY_ROOT = $Root
if (-not $env:OLLAMA_HOST) { $env:OLLAMA_HOST = 'http://127.0.0.1:11434' }
if (-not $env:OLLAMA_MODEL) { $env:OLLAMA_MODEL = 'qwen3:8b' }

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host 'Creating Python virtual environment...'
    $venvCode = Invoke-Python -Python $Python -Arguments @('-m', 'venv', '.venv')
    if ($venvCode -ne 0) { exit $venvCode }
}

& $VenvPython '-c' 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'The existing .venv was created with an older Python.' -ForegroundColor Red
    Write-Host 'Run: Remove-Item -Recurse -Force .venv'
    Write-Host 'Then run: .\start.cmd'
    exit 1
}

Write-Host 'Installing/updating backend dependencies...'
& $VenvPython '-m' 'pip' 'install' '--quiet' '--upgrade' 'pip'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPython '-m' 'pip' 'install' '--quiet' '-e' './backend[dev]'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path 'frontend\node_modules')) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location 'frontend'
    try {
        & npm.cmd 'install' '--no-audit' '--no-fund'
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally {
        Pop-Location
    }
}

if (-not (Test-Path 'frontend\public')) {
    New-Item -ItemType Directory -Path 'frontend\public' | Out-Null
}
if (Test-Path 'activity-map.html') {
    Copy-Item 'activity-map.html' 'frontend\public\activity-map.html' -Force
}

$OllamaCommand = Get-Command ollama.exe -ErrorAction SilentlyContinue
if (-not $OllamaCommand) {
    $OllamaDefault = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $OllamaDefault) { $OllamaCommand = Get-Item $OllamaDefault }
}

if ($OllamaCommand) {
    try {
        $ollamaExe = $OllamaCommand.Source
        if (-not $ollamaExe) { $ollamaExe = $OllamaCommand.FullName }
        $models = & $ollamaExe 'list' 2>$null
        $foundModel = $false
        foreach ($line in $models) {
            if ($line -match '^\s*([^\s]+)') {
                if ($Matches[1] -eq $env:OLLAMA_MODEL) {
                    $foundModel = $true
                    break
                }
            }
        }
        if ($foundModel) {
            Write-Host "Found Ollama model $($env:OLLAMA_MODEL)"
        } else {
            Write-Host "Ollama is installed, but $($env:OLLAMA_MODEL) is not installed." -ForegroundColor Yellow
            Write-Host "Install it with: ollama pull $($env:OLLAMA_MODEL)"
        }
    } catch {
        Write-Host 'Ollama is installed but is not responding yet.' -ForegroundColor Yellow
        Write-Host 'Open Ollama, then run start.cmd again.'
    }
} else {
    Write-Host 'Ollama is not installed or could not be found.' -ForegroundColor Yellow
}

function Stop-StaleLocalCompanyWorkers {
    try {
        $rootEscaped = [regex]::Escape($Root)
        $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
        foreach ($process in $processes) {
            if ($process.ProcessId -eq $PID) { continue }
            if (-not $process.CommandLine) { continue }
            if ($process.CommandLine -notmatch $rootEscaped) { continue }
            if ($process.Name -notmatch '^(python|pythonw|node)(\.exe)?$') { continue }
            Write-Host "Stopping stale Local Company worker $($process.ProcessId)..."
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Host 'Could not inspect stale Local Company workers; continuing.' -ForegroundColor Yellow
    }
    Start-Sleep -Milliseconds 500
}

Stop-StaleLocalCompanyWorkers

Write-Host 'Initializing persistent company database...'
& $VenvPython (Join-Path $Root 'run_backend.py') '--seed'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $VenvPython '-c' 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()' 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing Playwright Chromium...'
    & $VenvPython '-m' 'playwright' 'install' 'chromium'
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$Backend = $null
$Frontend = $null
try {
    $Backend = Start-Process -FilePath $VenvPython -ArgumentList @((Join-Path $Root 'run_backend.py')) -WorkingDirectory $Root -NoNewWindow -PassThru
    $Frontend = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1') -WorkingDirectory (Join-Path $Root 'frontend') -NoNewWindow -PassThru

    Write-Host ''
    Write-Host 'Local Company is starting:' -ForegroundColor Green
    Write-Host '  UI:           http://127.0.0.1:5173'
    Write-Host '  Activity map: http://127.0.0.1:5173/activity-map.html'
    Write-Host '  Backend:      http://127.0.0.1:8000'
    Write-Host 'Press Ctrl-C to stop both servers.'
    Write-Host ''

    while ($true) {
        Start-Sleep -Seconds 1
        $Backend.Refresh()
        $Frontend.Refresh()
        if ($Backend.HasExited -or $Frontend.HasExited) { break }
    }

    if ($Backend.HasExited) {
        Write-Host "Backend exited with code $($Backend.ExitCode)." -ForegroundColor Red
    }
    if ($Frontend.HasExited) {
        Write-Host "Frontend exited with code $($Frontend.ExitCode)." -ForegroundColor Red
    }
} finally {
    if ($Backend -and -not $Backend.HasExited) {
        Stop-Process -Id $Backend.Id -Force -ErrorAction SilentlyContinue
    }
    if ($Frontend -and -not $Frontend.HasExited) {
        Stop-Process -Id $Frontend.Id -Force -ErrorAction SilentlyContinue
    }
}
