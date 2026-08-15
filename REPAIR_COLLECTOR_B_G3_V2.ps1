#requires -Version 5.1
<#
Collector B G3 repair V2.

1) Runs the corrected clock-aware G2/G3 audit first.
2) Refuses any destructive/epoch action if a non-reconnect integrity failure remains.
3) If clean or reconnect-only, delegates to the existing conservative Gap Guardian installer.

No LIVE, no private API, no order path, no Collector A mutation.
#>
$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Data = "C:\TradingCore_Collector_B\data"
$Py = "C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
$Latest = "$Repo\collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json"
$GuardianInstaller = "$Repo\INSTALL_COLLECTOR_B_G3_GAP_GUARDIAN.ps1"

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "G3 V2 REPAIR STOPPED SAFELY" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Collector/PAPER remain running. LIVE remains disabled." -ForegroundColor Green
    exit 1
}

if (-not (Test-Path $Py)) { Fail "Collector B Python missing: $Py" }
if (-not (Test-Path "$Repo\collector_b_g2_g3_audit.py")) { Fail "Corrected G2/G3 audit missing." }
if (-not (Test-Path $GuardianInstaller)) { Fail "Gap Guardian installer missing." }

$env:TRADING_ENVIRONMENT = "PAPER"
$env:LIVE_TRADING = "false"
$env:PAPER_TRADING = "true"
$env:DEMO_ONLY = "true"
$env:COLLECTOR_B_DATA_DIR = $Data

Write-Host ""
Write-Host "=== COLLECTOR B G3 REPAIR V2 ===" -ForegroundColor Cyan
Write-Host "Running clock-aware fresh audit..."

Push-Location $Repo
try {
    & $Py .\collector_b_g2_g3_audit.py --data-dir $Data
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($Code -ne 0) { Fail "Fresh G2/G3 audit failed." }

$Audit = Read-JsonSafe $Latest
if (-not $Audit) { Fail "Fresh audit JSON not found." }

$Failures = @($Audit.g3.failures | Where-Object { $_ })
$NonReconnect = @($Failures | Where-Object { $_ -ne "RECONNECT_GAP_ACCOUNTING_REQUIRED" })

Write-Host ""
Write-Host "Fresh G2: $($Audit.g2.state)"
Write-Host "Fresh G3: $($Audit.g3.state)"
Write-Host "Timestamp hard anomalies: $($Audit.evidence.timestamp_order_anomalies)"
Write-Host "Clock offset ms: $($Audit.server_clock_probe.local_to_bybit_offset_ms)"
Write-Host "Failures: $($Failures -join ',')"
Write-Host "Pending: $(@($Audit.g3.pending) -join ',')"

if ($Audit.g2.state -ne "G2_PASS") {
    Fail "G2 is no longer PASS; no automatic repair allowed."
}
if ($NonReconnect.Count -gt 0) {
    Fail ("Non-reconnect G3 failures remain: " + ($NonReconnect -join ", "))
}

Write-Host ""
if ($Failures -contains "RECONNECT_GAP_ACCOUNTING_REQUIRED") {
    Write-Host "Only reconnect-gap remains. Installing guardian and rotating to a clean epoch..." -ForegroundColor Cyan
} else {
    Write-Host "No hard G3 integrity failures remain. Installing guardian for future reconnects..." -ForegroundColor Cyan
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $GuardianInstaller
if ($LASTEXITCODE -ne 0) { Fail "Gap Guardian installation failed." }

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " G3 V2 REPAIR COMPLETE" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Clock-aware audit: ACTIVE"
Write-Host "Gap Guardian: ACTIVE"
Write-Host "Collector A: UNCHANGED"
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
