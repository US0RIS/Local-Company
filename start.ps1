$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$ExpectedBundleSha256 = 'be4b46495a0a44380770ae694769ed584073088b3fbf0399616c93f4800e01e9'

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory=$true)][string]$Exe,
        [string[]]$PrefixArgs = @()
    )
    try {
        & $Exe @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @()

    if ($env:PYTHON_BIN) {
        $candidates += ,@($env:PYTHON_BIN, @())
    }

    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        foreach ($version in @('-3.14','-3.13','-3.12')) {
            $candidates += ,@('py.exe', @($version))
        }
    }

    foreach ($name in @('python3.14.exe','python3.13.exe','python3.12.exe','python.exe')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $candidates += ,@($name, @())
        }
    }

    foreach ($candidate in $candidates) {
        $exe = [string]$candidate[0]
        $prefix = [string[]]$candidate[1]
        if (Test-PythonCandidate -Exe $exe -PrefixArgs $prefix) {
            return [pscustomobject]@{ Exe = $exe; PrefixArgs = $prefix }
        }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host 'Python 3.12+ is required, but no compatible interpreter was found.' -ForegroundColor Red
    Write-Host 'Install Python 3.12+ from https://www.python.org/downloads/windows/ and enable the Python Launcher.'
    Write-Host 'Then reopen Terminal and run start.cmd again.'
    exit 1
}

$PyVersion = (& $Python.Exe @($Python.PrefixArgs) --version 2>&1 | Out-String).Trim()
Write-Host "✓ Using $PyVersion"

function Invoke-SelectedPython {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
    & $Python.Exe @($Python.PrefixArgs) @Args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

function Bootstrap-Source {
    if ((Test-Path 'backend\app\main.py') -and (Test-Path 'frontend\package.json')) { return }

    Write-Host 'Preparing Local Company source from the repository bootstrap bundle...'
    $script = @'
import base64
import hashlib
import io
import pathlib
import sys
import tarfile

root = pathlib.Path(sys.argv[1]).resolve()
expected = sys.argv[2]
parts = sorted((root / "bootstrap").glob("bundle.part.*"))
if not parts:
    raise SystemExit("Bootstrap bundle is missing. Re-clone US0RIS/Local-Company and try again.")
payload = b"".join(part.read_bytes().strip() for part in parts)
archive = base64.b64decode(payload, validate=True)
actual = hashlib.sha256(archive).hexdigest()
if actual != expected:
    raise SystemExit(f"Bootstrap checksum mismatch. Expected {expected}, got {actual}. Re-clone the repository.")
with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
    for member in tf.getmembers():
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise SystemExit(f"Unsafe path in bootstrap archive: {member.name}")
    tf.extractall(root)
required = [root / "backend/app/main.py", root / "frontend/package.json", root / "docs/ARCHITECTURE.md"]
missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
if missing:
    raise SystemExit("Bootstrap completed incompletely; missing: " + ", ".join(missing))
print("Source ready.")
'@
    $tempScript = Join-Path $env:TEMP ('local-company-bootstrap-' + [guid]::NewGuid().ToString('N') + '.py')
    try {
        [IO.File]::WriteAllText($tempScript, $script, [Text.UTF8Encoding]::new($false))
        Invoke-SelectedPython $tempScript $Root $ExpectedBundleSha256
    } finally {
        Remove-Item $tempScript -Force -ErrorAction SilentlyContinue
    }
}

Bootstrap-Source

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    Write-Host 'Node.js/npm is required for the UI.' -ForegroundColor Red
    Write-Host 'Install Node.js 20+ from https://nodejs.org/ and run start.cmd again.'
    exit 1
}

if (Test-Path '.env') {
    Get-Content '.env' | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
        $name, $value = $line -split '=', 2
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name) { [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
    }
}

$env:LOCAL_COMPANY_ROOT = $Root
if (-not $env:OLLAMA_HOST) { $env:OLLAMA_HOST = 'http://127.0.0.1:11434' }
if (-not $env:OLLAMA_MODEL) { $env:OLLAMA_MODEL = 'qwen3:8b' }

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Creating Python virtual environment...'
    Invoke-SelectedPython -m venv .venv
}

$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
& $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'The existing .venv uses Python older than 3.12.' -ForegroundColor Red
    Write-Host 'Delete the .venv folder manually, then run start.cmd again.'
    exit 1
}

Write-Host 'Installing/updating backend dependencies...'
& $VenvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPython -m pip install --quiet -e './backend[dev]'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path 'frontend\node_modules')) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location frontend
    try {
        & npm.cmd install --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally { Pop-Location }
}

New-Item -ItemType Directory -Force -Path 'frontend\public' | Out-Null
if (Test-Path 'activity-map.html') {
    Copy-Item 'activity-map.html' 'frontend\public\activity-map.html' -Force
}

if (Get-Command ollama.exe -ErrorAction SilentlyContinue) {
    try {
        $models = & ollama.exe list 2>$null
        $found = $false
        foreach ($line in $models) {
            if ($line -match '^\s*([^\s]+)' -and $Matches[1] -eq $env:OLLAMA_MODEL) { $found = $true; break }
        }
        if ($found) {
            Write-Host "✓ Found Ollama model $($env:OLLAMA_MODEL)"
        } else {
            Write-Host "! Ollama is installed, but $($env:OLLAMA_MODEL) was not found." -ForegroundColor Yellow
            Write-Host '  Verify installed models with: ollama list'
            Write-Host '  Local Company will not download a model automatically.'
        }
    } catch {
        Write-Host '! Ollama is installed but its local service could not be queried.' -ForegroundColor Yellow
        Write-Host '  Start Ollama, then use Test Model in the UI.'
    }
} else {
    Write-Host '! Ollama is not on PATH. Local Company will still start.' -ForegroundColor Yellow
    Write-Host '  Install/start Ollama for Windows and then use Test Model in the UI.'
}

function Stop-StaleLocalCompanyProcesses {
    try {
        $rootEscaped = [regex]::Escape($Root)
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $PID -and $_.CommandLine -and
                ($_.CommandLine -match $rootEscaped) -and
                ($_.Name -match '^(python|pythonw|node|npm|cmd|powershell|pwsh)(\.exe)?$')
            } |
            ForEach-Object {
                Write-Host "Stopping stale Local Company process $($_.ProcessId)..."
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
    } catch {
        Write-Host '! Could not inspect stale processes; continuing.' -ForegroundColor Yellow
    }
    Start-Sleep -Milliseconds 600
}

Stop-StaleLocalCompanyProcesses

Write-Host 'Initializing persistent company database...'
& $VenvPython (Join-Path $Root 'run_backend.py') --seed
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $VenvPython -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing Playwright Chromium...'
    & $VenvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$Backend = $null
$Frontend = $null
try {
    $Backend = Start-Process -FilePath $VenvPython -ArgumentList @((Join-Path $Root 'run_backend.py')) -WorkingDirectory $Root -NoNewWindow -PassThru
    $Frontend = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run','dev','--','--host','127.0.0.1') -WorkingDirectory (Join-Path $Root 'frontend') -NoNewWindow -PassThru

    Write-Host ''
    Write-Host 'Local Company is starting:' -ForegroundColor Green
    Write-Host '  UI:           http://127.0.0.1:5173'
    Write-Host '  Activity map: http://127.0.0.1:5173/activity-map.html'
    Write-Host '  Backend:      http://127.0.0.1:8000'
    Write-Host 'Press Ctrl-C to stop both servers.'
    Write-Host ''

    while (-not $Backend.HasExited -and -not $Frontend.HasExited) {
        Start-Sleep -Seconds 1
        $Backend.Refresh()
        $Frontend.Refresh()
    }

    if ($Backend.HasExited) { Write-Host "Backend exited with code $($Backend.ExitCode)." -ForegroundColor Red }
    if ($Frontend.HasExited) { Write-Host "Frontend exited with code $($Frontend.ExitCode)." -ForegroundColor Red }
} finally {
    foreach ($proc in @($Backend,$Frontend)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
