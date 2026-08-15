#requires -Version 5.1
<#
Collector B G3 Gap Guardian.

Purpose:
- preserve every captured record;
- if G3 is blocked ONLY because a WebSocket reconnect creates an unverifiable
  gap, archive the current active evidence as a separate epoch;
- reset only the current-epoch reconnect counter;
- restart Collector B on a fresh active evidence directory;
- repeat the same conservative rotation automatically after future reconnects.

No evidence is deleted. No strategy/outcome code is run here. No private API,
orders, LIVE trading, or Collector A access.
#>

$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Root = "C:\TradingCore_Collector_B"
$Data = "$Root\data"
$Control = "$Root\control"
$Logs = "$Root\logs"
$Py = "$Root\.venv\Scripts\python.exe"
$CollectorTask = "TradingCore Collector B"
$AuditTask = "TradingCore Collector B G2 G3 Auto Audit"
$GuardianTask = "TradingCore Collector B G3 Gap Guardian"
$Guardian = "$Control\G3_GAP_GUARDIAN.ps1"
$Ledger = "$Data\epochs\G3_EPOCH_LEDGER.jsonl"
$GuardianStatus = "$Data\G3_GAP_GUARDIAN_STATUS.json"
$LatestAudit = "$Repo\collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json"

function Write-AtomicJson([string]$Path, [object]$Payload) {
    $Dir = Split-Path -Parent $Path
    if ($Dir) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
    $Tmp = "$Path.tmp"
    $Payload | ConvertTo-Json -Depth 30 | Set-Content $Tmp -Encoding UTF8
    Move-Item $Tmp $Path -Force
}

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Fail([string]$Text) {
    Write-Host ""
    Write-Host "G3 GAP GUARDIAN NOT INSTALLED" -ForegroundColor Red
    Write-Host $Text -ForegroundColor Yellow
    Write-Host "Existing PAPER/research services were left in their current state." -ForegroundColor Green
    exit 1
}

if (-not (Test-Path $Repo)) { Fail "TradingCore recovery repo missing: $Repo" }
if (-not (Test-Path $Data)) { Fail "Collector B data directory missing: $Data" }
if (-not (Test-Path $Py)) { Fail "Collector B Python missing: $Py" }
if (-not (Test-Path "$Repo\collector_b_g2_g3_audit.py")) { Fail "G2/G3 audit script missing." }

# Freeze current failure reason before any action. Only reconnect-gap repair is
# allowed automatically. Any duplicate/timestamp/schema issue requires a
# different repair and must not be hidden by epoch rotation.
$Audit = Read-JsonSafe $LatestAudit
if (-not $Audit) {
    Push-Location $Repo
    try {
        $env:TRADING_ENVIRONMENT = "PAPER"
        $env:LIVE_TRADING = "false"
        $env:PAPER_TRADING = "true"
        $env:DEMO_ONLY = "true"
        $env:COLLECTOR_B_DATA_DIR = $Data
        & $Py .\collector_b_g2_g3_audit.py --data-dir $Data | Out-Null
    } finally { Pop-Location }
    $Audit = Read-JsonSafe $LatestAudit
}

if (-not $Audit) { Fail "Could not obtain current G2/G3 audit." }

$Failures = @($Audit.g3.failures | Where-Object { $_ })
$NonReconnect = @($Failures | Where-Object { $_ -ne "RECONNECT_GAP_ACCOUNTING_REQUIRED" })
if ($NonReconnect.Count -gt 0) {
    Fail ("G3 has non-reconnect failures: " + ($NonReconnect -join ", ") + ". Epoch rotation intentionally refused.")
}

# Guardian body. It rotates only when reconnect_count > 0. A reconnect count is
# current-epoch state: after a successful archive it is reset to zero while the
# archived value is retained in the append-only epoch ledger.
@'
$ErrorActionPreference = "Continue"
$Root = "C:\TradingCore_Collector_B"
$Data = "$Root\data"
$Logs = "$Root\logs"
$CollectorTask = "TradingCore Collector B"
$Ledger = "$Data\epochs\G3_EPOCH_LEDGER.jsonl"
$GuardianStatus = "$Data\G3_GAP_GUARDIAN_STATUS.json"
$Lock = "$Data\epochs\.rotation.lock"

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Write-AtomicJson([string]$Path, [object]$Payload) {
    $Dir = Split-Path -Parent $Path
    if ($Dir) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
    $Tmp = "$Path.tmp"
    $Payload | ConvertTo-Json -Depth 30 | Set-Content $Tmp -Encoding UTF8
    Move-Item $Tmp $Path -Force
}

New-Item -ItemType Directory -Force -Path "$Data\epochs",$Logs | Out-Null

if (Test-Path $Lock) {
    $AgeMin = ((Get-Date) - (Get-Item $Lock).LastWriteTime).TotalMinutes
    if ($AgeMin -lt 10) { exit 0 }
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType File -Force -Path $Lock | Out-Null

try {
    $Status = Read-JsonSafe "$Data\status.json"
    $Checkpoint = Read-JsonSafe "$Data\checkpoint.json"
    if (-not $Status -or -not $Checkpoint) {
        Write-AtomicJson $GuardianStatus @{
            state="WAITING_FOR_COLLECTOR_STATUS"; updated_at_utc=(Get-Date).ToUniversalTime().ToString("o"); real_orders_enabled=$false
        }
        exit 0
    }

    $Reconnects = [int]($Status.reconnect_count)
    if ($Reconnects -le 0) {
        Write-AtomicJson $GuardianStatus @{
            state="HEALTHY_NO_GAP"; reconnect_count_current_epoch=$Reconnects; events_written_lifetime=$Status.events_written;
            updated_at_utc=(Get-Date).ToUniversalTime().ToString("o"); collector_a_modified=$false; real_orders_enabled=$false
        }
        exit 0
    }

    $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
    $EpochDir = "$Data\epochs\EPOCH_CLOSED_RECONNECT_$Stamp"
    New-Item -ItemType Directory -Force -Path $EpochDir | Out-Null

    # Stop only Collector B while rotating its active evidence directories.
    try { Stop-ScheduledTask -TaskName $CollectorTask -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 3
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*collector_b_bybit.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2

    # Re-read checkpoint after collector is stopped.
    $Checkpoint = Read-JsonSafe "$Data\checkpoint.json"
    $Status = Read-JsonSafe "$Data\status.json"

    foreach ($Relative in @("normalized\bybit","raw\bybit","manifests")) {
        $Source = Join-Path $Data $Relative
        if (Test-Path $Source) {
            $SafeName = $Relative -replace '[\\/]', '_'
            Move-Item $Source (Join-Path $EpochDir $SafeName) -Force
        }
    }

    if (Test-Path "$Data\status.json") { Copy-Item "$Data\status.json" "$EpochDir\status_before_rotation.json" -Force }
    if (Test-Path "$Data\checkpoint.json") { Copy-Item "$Data\checkpoint.json" "$EpochDir\checkpoint_before_rotation.json" -Force }

    $LedgerRow = [ordered]@{
        schema="TRADINGCORE_COLLECTOR_B_G3_EPOCH_LEDGER_V1"
        closed_at_utc=(Get-Date).ToUniversalTime().ToString("o")
        reason="WEBSOCKET_RECONNECT_GAP_UNBACKFILLABLE"
        archive_path=$EpochDir
        reconnect_count_at_close=[int]($Status.reconnect_count)
        events_written_lifetime_at_close=[int]($Status.events_written)
        symbol_counts=$Status.symbol_counts
        last_event_ts_ms=$Status.last_event_ts_ms
        evidence_preserved=$true
        collector_a_modified=$false
        real_orders_enabled=$false
    }
    ($LedgerRow | ConvertTo-Json -Depth 20 -Compress) | Add-Content $Ledger -Encoding UTF8

    # Reset ONLY the current-epoch reconnect counter. Lifetime evidence counters
    # remain intact. Active normalized/raw directories are recreated by collector.
    $Checkpoint.reconnect_count = 0
    $Checkpoint.last_error = $null
    $Checkpoint.previous_epoch_archive = $EpochDir
    $Checkpoint.current_evidence_epoch_started_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    Write-AtomicJson "$Data\checkpoint.json" $Checkpoint

    try { Start-ScheduledTask -TaskName $CollectorTask -ErrorAction Stop } catch {
        Write-AtomicJson $GuardianStatus @{
            state="COLLECTOR_RESTART_FAILED"; archive_path=$EpochDir; error=$_.Exception.Message;
            updated_at_utc=(Get-Date).ToUniversalTime().ToString("o"); real_orders_enabled=$false
        }
        exit 1
    }

    Start-Sleep -Seconds 8
    $After = Read-JsonSafe "$Data\status.json"
    Write-AtomicJson $GuardianStatus @{
        state="NEW_CLEAN_EPOCH_STARTED"
        archive_path=$EpochDir
        connection_state=$After.connection_state
        reconnect_count_current_epoch=$After.reconnect_count
        events_written_lifetime=$After.events_written
        current_evidence_epoch_started_at_utc=$Checkpoint.current_evidence_epoch_started_at_utc
        updated_at_utc=(Get-Date).ToUniversalTime().ToString("o")
        collector_a_modified=$false
        real_orders_enabled=$false
    }
} finally {
    Remove-Item $Lock -Force -ErrorAction SilentlyContinue
}
'@ | Set-Content $Guardian -Encoding UTF8

$Tokens = $null
$Errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($Guardian,[ref]$Tokens,[ref]$Errors)
if ($Errors.Count -gt 0) { Fail "Generated guardian syntax check failed." }

# Install/replace guardian scheduled task, every 5 minutes, current user only.
try {
    $Existing = Get-ScheduledTask -TaskName $GuardianTask -ErrorAction SilentlyContinue
    if ($Existing) { Unregister-ScheduledTask -TaskName $GuardianTask -Confirm:$false -ErrorAction Stop }
    $User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Guardian`""
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $GuardianTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
} catch { Fail "Could not install guardian scheduled task: $($_.Exception.Message)" }

# Immediate rotation only if reconnect gap is currently present.
$StatusNow = Read-JsonSafe "$Data\status.json"
if ($StatusNow -and [int]$StatusNow.reconnect_count -gt 0) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Guardian
    if ($LASTEXITCODE -ne 0) { Fail "Immediate G3 gap rotation failed." }
}

# Fresh audit after clean-era rotation. It should now be PENDING_SAMPLE, not
# REPAIR_REQUIRED, unless a different integrity problem exists.
Start-Sleep -Seconds 5
Push-Location $Repo
try {
    $env:TRADING_ENVIRONMENT = "PAPER"
    $env:LIVE_TRADING = "false"
    $env:PAPER_TRADING = "true"
    $env:DEMO_ONLY = "true"
    $env:COLLECTOR_B_DATA_DIR = $Data
    & $Py .\collector_b_g2_g3_audit.py --data-dir $Data
} finally { Pop-Location }

$Fresh = Read-JsonSafe $LatestAudit
$Guard = Read-JsonSafe $GuardianStatus

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " COLLECTOR B G3 GAP GUARDIAN INSTALLED" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host "Guardian: every 5 minutes / automatic epoch rotation"
Write-Host "Previous evidence: PRESERVED in C:\TradingCore_Collector_B\data\epochs"
Write-Host "Collector A: UNCHANGED" -ForegroundColor Green
Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
if ($Guard) { Write-Host "Guardian state: $($Guard.state)" }
if ($Fresh) {
    Write-Host "G2: $($Fresh.g2.state)"
    Write-Host "G3: $($Fresh.g3.state)"
    Write-Host "G3 failures: $(@($Fresh.g3.failures) -join ',')"
    Write-Host "G3 pending: $(@($Fresh.g3.pending) -join ',')"
    Write-Host "Active epoch events: $($Fresh.evidence.valid_unique_events)"
    Write-Host "Active epoch span hours: $($Fresh.evidence.observation_span_hours)"
}
