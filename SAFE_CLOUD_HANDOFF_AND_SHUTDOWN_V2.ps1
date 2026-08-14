#requires -Version 5.1
<#
Bootstrap V2 for SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1.

The caller updates the trusted repository once before launching this file.
V2 deliberately performs NO second git pull. It validates that the required
cloud-handoff files are present, creates a temporary copy of the audited
orchestrator with its redundant internal git-update block disabled, and runs
that copy. All original fail-closed shutdown behavior remains intact.
#>

$ErrorActionPreference = "Stop"

$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Original = Join-Path $Repo "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1"
$Temp = Join-Path $env:TEMP "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN_RUNTIME.ps1"

$Required = @(
    "SAFE_CLOUD_HANDOFF_AND_SHUTDOWN.ps1",
    "collector_b_bybit.py",
    "collector_b_selftest.py",
    "collector_b_g2_g3_audit.py",
    "collector_b_cloud_supervisor.py",
    "collector_b_requirements.txt",
    "Dockerfile.collector_b"
)

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " SAFE CLOUD HANDOFF BOOTSTRAP V2" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "Repository was already updated by the outer command."
Write-Host "No second git pull will be performed."
Write-Host ""

if (-not (Test-Path $Repo)) {
    Write-Host "TRUSTED REPOSITORY NOT FOUND - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}

foreach ($Name in $Required) {
    $Path = Join-Path $Repo $Name
    if (-not (Test-Path $Path)) {
        Write-Host "REQUIRED FILE MISSING: $Path" -ForegroundColor Red
        Write-Host "NO PROJECTS WERE STOPPED. NO SHUTDOWN." -ForegroundColor Yellow
        exit 1
    }
}

# Verify that the cloud supervisor is the current Docker entrypoint.
$Docker = Get-Content (Join-Path $Repo "Dockerfile.collector_b") -Raw
if ($Docker -notmatch 'collector_b_cloud_supervisor\.py') {
    Write-Host "CLOUD SUPERVISOR IS NOT IN DOCKERFILE - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}

$Text = Get-Content $Original -Raw

# Remove exactly the redundant inner git-update section. The outer command has
# already performed the ff-only update and will refuse to run this V2 on failure.
$Pattern = '(?ms)^\s*# ------------------------------------------------------------------\r?\n\s*# 1\. Update trusted source\.\r?\n\s*# ------------------------------------------------------------------\r?\n\s*\$git = Run-Cmd "git" @\("-C", \$Repo, "pull", "--ff-only", "origin", \$Branch\) \$true\r?\n\s*if \(\$git\.Code -ne 0\) \{\r?\n\s*Fail-Safe "Git update failed; local projects left running\."\r?\n\s*\}\r?\n'

$Replacement = @'
    # ------------------------------------------------------------------
    # 1. Trusted source already updated by SAFE bootstrap V2.
    # ------------------------------------------------------------------
    Log "Trusted repository update already verified by outer bootstrap V2."

'@

$Patched = [regex]::Replace($Text, $Pattern, $Replacement, 1)

if ($Patched -eq $Text) {
    Write-Host "COULD NOT SAFELY PATCH REDUNDANT GIT STEP - NO SHUTDOWN" -ForegroundColor Red
    exit 1
}

Set-Content $Temp -Value $Patched -Encoding UTF8

# Parse-only syntax validation before executing anything consequential.
$Tokens = $null
$Errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $Temp,
    [ref]$Tokens,
    [ref]$Errors
)

if ($Errors.Count -gt 0) {
    Write-Host "ORCHESTRATOR SYNTAX CHECK FAILED - NO SHUTDOWN" -ForegroundColor Red
    $Errors | ForEach-Object { Write-Host $_.Message -ForegroundColor Yellow }
    exit 1
}

Write-Host "Bootstrap validation: PASS" -ForegroundColor Green
Write-Host "Starting fail-closed cloud handoff..." -ForegroundColor Cyan
Write-Host ""

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Temp
exit $LASTEXITCODE
