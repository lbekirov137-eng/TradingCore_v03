#requires -Version 5.1
<#
TradingCore 5-hour overnight supervisor — CONTINUE AFTER 5H.

Purpose:
- keep the laptop awake continuously;
- keep TradingCore PAPER, BTC 1H shadow and Collector B alive;
- run G2/G3 audit immediately and then hourly during the first 5 hours;
- preserve AI Media Factory and LinguaPilot if they are already running, but do
  not invent/start unknown workflows;
- save a five-hour report;
- AFTER five hours: DO NOT stop projects, DO NOT shut Windows down;
- continue in lightweight keep-alive mode so the laptop and trading research
  remain running.

No LIVE trading. No private exchange API. No strategy search is launched.
#>

$ErrorActionPreference = "Continue"
$DurationHours = 5
$PollSeconds = 300
$PostFiveHourPollSeconds = 900
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$CollectorRoot = "C:\TradingCore_Collector_B"
$CollectorData = "$CollectorRoot\data"
$CollectorPy = "$CollectorRoot\.venv\Scripts\python.exe"
$RunRoot = "C:\TradingCore_Overnight"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RunDir = Join-Path $RunRoot $Stamp
$Log = Join-Path $RunDir "OVERNIGHT.log"
$StatusFile = Join-Path $RunDir "STATUS.json"
$FinalFile = Join-Path $RunDir "FIVE_HOUR_REPORT.json"
$ContinueFile = Join-Path $RunRoot "CURRENT_CONTINUOUS_STATUS.json"

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

function Log([string]$Text) {
    $line = "{0}  {1}" -f ((Get-Date).ToUniversalTime().ToString("o")), $Text
    $line | Tee-Object -FilePath $Log -Append
}

function Ensure-Task([string]$Name) {
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        if (-not $task) {
            Log "TASK MISSING: $Name"
            return $false
        }
        if ($task.State -ne "Running") {
            Log "Starting task: $Name (state=$($task.State))"
            Start-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
        }
        return $true
    } catch {
        Log "TASK ERROR ${Name}: $($_.Exception.Message)"
        return $false
    }
}

function Main-Paper-Status {
    try {
        return (Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5)
    } catch {
        return $null
    }
}

function Read-JsonSafe([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    try { return (Get-Content $Path -Raw | ConvertFrom-Json) } catch { return $null }
}

function Run-G2G3-Audit {
    if (-not (Test-Path $CollectorPy)) {
        Log "G2/G3 audit skipped: isolated Collector B Python missing"
        return $null
    }

    $env:TRADING_ENVIRONMENT = "PAPER"
    $env:LIVE_TRADING = "false"
    $env:PAPER_TRADING = "true"
    $env:DEMO_ONLY = "true"
    $env:COLLECTOR_B_DATA_DIR = $CollectorData

    Push-Location $Repo
    try {
        & $CollectorPy .\collector_b_g2_g3_audit.py --data-dir $CollectorData *>> $Log
    } catch {
        Log "G2/G3 audit exception: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }

    return Read-JsonSafe (Join-Path $Repo "collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json")
}

function Snapshot-ProjectTasks([string]$Regex) {
    try {
        return @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
            $_.TaskName -match $Regex -or $_.TaskPath -match $Regex
        } | Select-Object TaskName,TaskPath,State)
    } catch { return @() }
}

function Ensure-Trading-Stack {
    [void](Ensure-Task "TradingCore PAPER 24x7")
    [void](Ensure-Task "TradingCore BTC 1H Forward Shadow")
    [void](Ensure-Task "TradingCore Collector B")
    [void](Ensure-Task "TradingCore Collector B G2 G3 Auto Audit")
}

function Get-Snapshot([object]$Audit, [string]$State, [datetime]$Started, [datetime]$Deadline) {
    $paper = Main-Paper-Status
    if (-not $paper -or $paper.running -ne $true -or $paper.real_orders_enabled -ne $false) {
        Log "Main PAPER unhealthy/unreachable; attempting scheduled-task restart"
        [void](Ensure-Task "TradingCore PAPER 24x7")
        Start-Sleep -Seconds 5
        $paper = Main-Paper-Status
    }

    $shadow = Read-JsonSafe "C:\TradingCore_BTC_1H_SHADOW\status.json"
    if (-not $shadow -or $shadow.running -ne $true -or $shadow.real_orders_enabled -ne $false) {
        Log "BTC 1H shadow status not healthy; ensuring task is running"
        [void](Ensure-Task "TradingCore BTC 1H Forward Shadow")
    }

    $collector = Read-JsonSafe "$CollectorData\status.json"
    if (-not $collector -or $collector.running -ne $true -or $collector.real_orders_enabled -ne $false) {
        Log "Collector B status not healthy; ensuring task is running"
        [void](Ensure-Task "TradingCore Collector B")
    }

    return @{
        state = $State
        started_at_local = $Started.ToString("o")
        five_hour_deadline_local = $Deadline.ToString("o")
        updated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        main_paper = $paper
        btc_1h_shadow = $shadow
        collector_b = $collector
        g2 = if ($Audit) { $Audit.g2.state } else { $null }
        g3 = if ($Audit) { $Audit.g3.state } else { $null }
        events = if ($Audit) { $Audit.evidence.valid_unique_events } else { $null }
        span_hours = if ($Audit) { $Audit.evidence.observation_span_hours } else { $null }
        ai_media_tasks = Snapshot-ProjectTasks '(?i)AI[_ ]?Media[_ ]?Factory|AI Media Factory'
        linguapilot_tasks = Snapshot-ProjectTasks '(?i)LinguaPilot|Language[ _-]?Pilot'
        real_orders_enabled = $false
        collector_a_modified = $false
        windows_shutdown_requested = $false
    }
}

# Cancel any previously scheduled Windows shutdown from an older supervisor.
& shutdown.exe /a 2>$null | Out-Null

# Prevent sleep while this supervisor is alive without changing the permanent
# Windows power plan.
try {
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class AwakeKeeper {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
    public const uint ES_CONTINUOUS = 0x80000000;
    public const uint ES_SYSTEM_REQUIRED = 0x00000001;
}
"@
[void][AwakeKeeper]::SetThreadExecutionState(
    [AwakeKeeper]::ES_CONTINUOUS -bor [AwakeKeeper]::ES_SYSTEM_REQUIRED
)
} catch {
    Log "Sleep-prevention warning: $($_.Exception.Message)"
}

$Started = Get-Date
$Deadline = $Started.AddHours($DurationHours)
$NextAudit = Get-Date

Log "5-hour intensive supervisor START"
Log "Five-hour checkpoint local: $($Deadline.ToString('o'))"
Log "NO SHUTDOWN after five hours; continuous mode will follow"
Log "LIVE trading remains disabled"

Ensure-Trading-Stack

# ---------------------------------------------------------------------------
# Phase 1: five-hour intensive monitoring, audit every hour.
# ---------------------------------------------------------------------------
while ((Get-Date) -lt $Deadline) {
    $now = Get-Date
    $audit = $null

    if ($now -ge $NextAudit) {
        Log "Running hourly G2/G3 audit"
        $audit = Run-G2G3-Audit
        $NextAudit = $now.AddHours(1)
    } else {
        $audit = Read-JsonSafe (Join-Path $Repo "collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json")
    }

    $snapshot = Get-Snapshot $audit "RUNNING_5H_INTENSIVE" $Started $Deadline
    $snapshot | ConvertTo-Json -Depth 20 | Set-Content $StatusFile -Encoding UTF8

    $paperOk = [bool]($snapshot.main_paper -and $snapshot.main_paper.running -eq $true)
    $shadowOk = [bool]($snapshot.btc_1h_shadow -and $snapshot.btc_1h_shadow.running -eq $true)
    $collectorOk = [bool]($snapshot.collector_b -and $snapshot.collector_b.running -eq $true)
    Log ("Heartbeat: PAPER={0} Shadow={1} Collector={2} G2={3} G3={4} Events={5}" -f $paperOk, $shadowOk, $collectorOk, $snapshot.g2, $snapshot.g3, $snapshot.events)

    $remaining = [int](($Deadline - (Get-Date)).TotalSeconds)
    if ($remaining -le 0) { break }
    Start-Sleep -Seconds ([Math]::Min($PollSeconds, $remaining))
}

# Five-hour checkpoint/report. Nothing is stopped here.
Log "Five-hour intensive window complete; running checkpoint audit"
$finalAudit = Run-G2G3-Audit
$fiveHour = Get-Snapshot $finalAudit "FIVE_HOUR_CHECKPOINT_COMPLETE_CONTINUING" $Started $Deadline
$fiveHour.outcome_research_started = $false
$fiveHour.note = "Five-hour intensive monitoring completed. Laptop/projects remain running. No shutdown requested."
$fiveHour | ConvertTo-Json -Depth 20 | Set-Content $FinalFile -Encoding UTF8
$fiveHour | ConvertTo-Json -Depth 20 | Set-Content $ContinueFile -Encoding UTF8

Log "Five-hour checkpoint saved. Switching to continuous keep-alive mode."
Log "TradingCore tasks continue. Windows remains ON."

# ---------------------------------------------------------------------------
# Phase 2: lightweight continuous keep-alive. No shutdown.
# G2/G3 scheduled task continues hourly; this loop checks health every 15 min.
# ---------------------------------------------------------------------------
while ($true) {
    Ensure-Trading-Stack

    $latestAudit = Read-JsonSafe (Join-Path $Repo "collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json")
    $continuous = Get-Snapshot $latestAudit "CONTINUOUS_AFTER_5H" $Started $Deadline
    $continuous.note = "Five-hour intensive window is complete. Laptop stays awake and projects continue running."
    $continuous | ConvertTo-Json -Depth 20 | Set-Content $ContinueFile -Encoding UTF8

    $paperOk = [bool]($continuous.main_paper -and $continuous.main_paper.running -eq $true)
    $shadowOk = [bool]($continuous.btc_1h_shadow -and $continuous.btc_1h_shadow.running -eq $true)
    $collectorOk = [bool]($continuous.collector_b -and $continuous.collector_b.running -eq $true)
    Log ("Continuous: PAPER={0} Shadow={1} Collector={2} G2={3} G3={4} Events={5}" -f $paperOk, $shadowOk, $collectorOk, $continuous.g2, $continuous.g3, $continuous.events)

    Start-Sleep -Seconds $PostFiveHourPollSeconds
}
