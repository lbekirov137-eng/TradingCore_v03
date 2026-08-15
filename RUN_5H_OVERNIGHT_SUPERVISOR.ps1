#requires -Version 5.1
<#
TradingCore 5-hour overnight supervisor.

Purpose:
- keep the laptop awake for 5 hours;
- keep TradingCore PAPER, BTC 1H shadow and Collector B alive;
- run G2/G3 audit immediately and then hourly;
- preserve AI Media Factory and LinguaPilot if they are already running, but do
  not invent/start unknown workflows;
- save a final report;
- request a normal Windows shutdown after five hours.

No LIVE trading. No private exchange API. No strategy search is launched.
#>

$ErrorActionPreference = "Continue"
$DurationHours = 5
$PollSeconds = 300
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$CollectorRoot = "C:\TradingCore_Collector_B"
$CollectorData = "$CollectorRoot\data"
$CollectorPy = "$CollectorRoot\.venv\Scripts\python.exe"
$RunRoot = "C:\TradingCore_Overnight"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RunDir = Join-Path $RunRoot $Stamp
$Log = Join-Path $RunDir "OVERNIGHT.log"
$StatusFile = Join-Path $RunDir "STATUS.json"
$FinalFile = Join-Path $RunDir "FINAL_REPORT.json"

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
        Log "TASK ERROR $Name: $($_.Exception.Message)"
        return $false
    }
}

function Main-Paper-Status {
    try {
        $s = Invoke-RestMethod "http://127.0.0.1:8001/monitor/status" -TimeoutSec 5
        return $s
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

    $latest = Join-Path $Repo "collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json"
    return Read-JsonSafe $latest
}

function Snapshot-ProjectTasks([string]$Regex) {
    try {
        return @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
            $_.TaskName -match $Regex -or $_.TaskPath -match $Regex
        } | Select-Object TaskName,TaskPath,State)
    } catch { return @() }
}

function Stop-For-Shutdown {
    Log "Beginning graceful shutdown sequence."

    # AI Media Factory: use its known stop script when available.
    $mediaStop = "C:\AI_Media_Factory\stop_autonomous_run.ps1"
    if (Test-Path $mediaStop) {
        Log "AI Media Factory graceful stop script"
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $mediaStop *>> $Log
        } catch { Log "AI Media stop warning: $($_.Exception.Message)" }
    }

    # LinguaPilot: prefer an existing stop script, if one exists.
    $languageStops = @(
        "C:\LinguaPilot\stop_linguapilot.ps1",
        "C:\LinguaPilot\STOP_LINGUAPILOT.ps1",
        "C:\LinguaPilot\stop.ps1",
        "C:\LinguaPilot\scripts\stop_linguapilot.ps1",
        "C:\LinguaPilot\scripts\stop.ps1"
    )
    $languageStop = $languageStops | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($languageStop) {
        Log "LinguaPilot graceful stop: $languageStop"
        try {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $languageStop *>> $Log
        } catch { Log "LinguaPilot stop warning: $($_.Exception.Message)" }
    }

    # Stop known TradingCore scheduled tasks, but DO NOT disable them permanently.
    foreach ($name in @(
        "TradingCore PAPER 24x7",
        "TradingCore BTC 1H Forward Shadow",
        "TradingCore Collector B",
        "TradingCore Collector B G2 G3 Auto Audit"
    )) {
        try {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($task -and $task.State -eq "Running") {
                Log "Stopping scheduled task: $name"
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            }
        } catch {}
    }

    Start-Sleep -Seconds 5
}

# Prevent Windows sleep while this supervisor is alive, without changing the
# user's permanent power plan.
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

Log "5-hour overnight supervisor START"
Log "Deadline local: $($Deadline.ToString('o'))"
Log "LIVE trading remains disabled"

# Initial task checks.
[void](Ensure-Task "TradingCore PAPER 24x7")
[void](Ensure-Task "TradingCore BTC 1H Forward Shadow")
[void](Ensure-Task "TradingCore Collector B")
[void](Ensure-Task "TradingCore Collector B G2 G3 Auto Audit")

while ((Get-Date) -lt $Deadline) {
    $now = Get-Date

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

    $audit = $null
    if ($now -ge $NextAudit) {
        Log "Running hourly G2/G3 audit"
        $audit = Run-G2G3-Audit
        $NextAudit = $now.AddHours(1)
    } else {
        $audit = Read-JsonSafe (Join-Path $Repo "collector_b_audit_results\LATEST_COLLECTOR_B_G2_G3.json")
    }

    $mediaTasks = Snapshot-ProjectTasks '(?i)AI[_ ]?Media[_ ]?Factory|AI Media Factory'
    $languageTasks = Snapshot-ProjectTasks '(?i)LinguaPilot|Language[ _-]?Pilot'

    $snapshot = @{
        state = "RUNNING_5H_OVERNIGHT"
        started_at_local = $Started.ToString("o")
        deadline_local = $Deadline.ToString("o")
        updated_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        main_paper = $paper
        btc_1h_shadow = $shadow
        collector_b = $collector
        g2 = if ($audit) { $audit.g2.state } else { $null }
        g3 = if ($audit) { $audit.g3.state } else { $null }
        events = if ($audit) { $audit.evidence.valid_unique_events } else { $null }
        span_hours = if ($audit) { $audit.evidence.observation_span_hours } else { $null }
        ai_media_tasks = $mediaTasks
        linguapilot_tasks = $languageTasks
        real_orders_enabled = $false
        collector_a_modified = $false
    }
    $snapshot | ConvertTo-Json -Depth 20 | Set-Content $StatusFile -Encoding UTF8

    Log ("Heartbeat: PAPER={0} Shadow={1} Collector={2} G2={3} G3={4} Events={5}" -f \
        ($paper -and $paper.running -eq $true), \
        ($shadow -and $shadow.running -eq $true), \
        ($collector -and $collector.running -eq $true), \
        $snapshot.g2, $snapshot.g3, $snapshot.events)

    $remaining = [int](($Deadline - (Get-Date)).TotalSeconds)
    if ($remaining -le 0) { break }
    Start-Sleep -Seconds ([Math]::Min($PollSeconds, $remaining))
}

Log "Five-hour window complete; running final audit"
$finalAudit = Run-G2G3-Audit
$finalPaper = Main-Paper-Status
$finalShadow = Read-JsonSafe "C:\TradingCore_BTC_1H_SHADOW\status.json"
$finalCollector = Read-JsonSafe "$CollectorData\status.json"

@{
    state = "FIVE_HOUR_WINDOW_COMPLETE"
    started_at_local = $Started.ToString("o")
    completed_at_local = (Get-Date).ToString("o")
    main_paper = $finalPaper
    btc_1h_shadow = $finalShadow
    collector_b = $finalCollector
    g2 = if ($finalAudit) { $finalAudit.g2.state } else { $null }
    g3 = if ($finalAudit) { $finalAudit.g3.state } else { $null }
    events = if ($finalAudit) { $finalAudit.evidence.valid_unique_events } else { $null }
    span_hours = if ($finalAudit) { $finalAudit.evidence.observation_span_hours } else { $null }
    outcome_research_started = $false
    note = "No new strategy/outcome research was started automatically. G2/G3 remain authoritative."
    real_orders_enabled = $false
    collector_a_modified = $false
} | ConvertTo-Json -Depth 20 | Set-Content $FinalFile -Encoding UTF8

Stop-For-Shutdown

try {
    [void][AwakeKeeper]::SetThreadExecutionState([AwakeKeeper]::ES_CONTINUOUS)
} catch {}

Log "Requesting normal Windows shutdown"
shutdown.exe /s /t 60 /c "Five-hour safe overnight work window complete. TradingCore reports saved to $RunDir"
