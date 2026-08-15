#requires -Version 5.1
<#
Run Historical Accelerator NOW using cached fast lookups, then install hidden
forward PAPER confirmation without reopening the historical holdout.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Root = "C:\TradingCore_Historical_Accelerator"
$Installer = "$Repo\INSTALL_HISTORICAL_ACCELERATOR.ps1"

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "HISTORICAL ACCELERATOR STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "V1/Wide V2 keep running. LIVE / real orders remain DISABLED." -ForegroundColor Green
    exit 1
}

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host " TRADINGCORE HISTORICAL ACCELERATOR - RUN NOW" -ForegroundColor Cyan
Write-Host "================================================================================" -ForegroundColor Cyan
Write-Host "730-day historical test. No waiting for live collectors. No terminal cleanup."

if (-not (Test-Path $Repo)) { Fail "Repo missing: $Repo" }
if (-not (Test-Path $Py)) { Fail "Python missing: $Py" }
if (-not (Test-Path $Installer)) { Fail "Installer missing: $Installer" }
New-Item -ItemType Directory -Force -Path $Root | Out-Null

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
@(
    "BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY",
    "BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY",
    "OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY"
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

Push-Location $Repo
try {
    Write-Host "Compile + safety self-test..." -ForegroundColor Cyan
    & $Py -m py_compile `
        .\historical_accelerator_protocol.py `
        .\historical_accelerator.py `
        .\historical_accelerator_fast.py `
        .\historical_accelerator_selftest.py `
        .\historical_accelerator_forward_paper.py
    if ($LASTEXITCODE -ne 0) { Fail "Python compile failed." }

    & $Py .\historical_accelerator_selftest.py
    if ($LASTEXITCODE -ne 0) { Fail "Self-test failed." }

    Write-Host ""
    Write-Host "Downloading/caching public history and running frozen holdout..." -ForegroundColor Cyan
    Write-Host "First run may take several minutes. Keep this terminal open until FINAL RESULT." -ForegroundColor Yellow
    & $Py .\historical_accelerator_fast.py --state-dir $Root
    if ($LASTEXITCODE -ne 0) { Fail "Historical engine returned non-zero." }
} finally {
    Pop-Location
}

$Decision = "$Root\HISTORICAL_DECISION_LOCK.json"
if (-not (Test-Path $Decision)) { Fail "Decision lock not created." }

# Historical decision is now sealed. The installer will see the decision lock,
# will NOT reopen the holdout, and will only finish/verify hidden forward PAPER.
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Installer
if ($LASTEXITCODE -ne 0) { Fail "Forward PAPER installation failed after historical decision." }

$D = Get-Content $Decision -Raw | ConvertFrom-Json
$F = $null
if (Test-Path "$Root\forward_paper_status.json") {
    try { $F = Get-Content "$Root\forward_paper_status.json" -Raw | ConvertFrom-Json } catch {}
}

Write-Host ""
Write-Host "================================================================================" -ForegroundColor Green
Write-Host " HISTORICAL ACCELERATOR NOW: COMPLETE" -ForegroundColor Green
Write-Host "================================================================================" -ForegroundColor Green
Write-Host "Verdict: $($D.state)"
Write-Host "Candidate: $($D.candidate_family)"
if ($F) { Write-Host "Forward PAPER: $($F.state) closed=$($F.closed_trades)" }
Write-Host "Live collectors: CONTINUE AS CONFIRMATION"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
Write-Host "Decision: $Decision"
Write-Host "================================================================================" -ForegroundColor Green
