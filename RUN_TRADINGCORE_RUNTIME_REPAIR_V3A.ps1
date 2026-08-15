#requires -Version 5.1
<#
Safe bootstrap for REPAIR_TRADINGCORE_RUNTIME_V3.ps1.
Patches only two PowerShell interpolation strings ($Name:) before parse/execute.
No project process is touched unless the patched V3 passes parser validation.
#>
$ErrorActionPreference = "Stop"
$Repo = "C:\TradingCore_Cloud_Recovery_20260814_135929"
$Source = Join-Path $Repo "REPAIR_TRADINGCORE_RUNTIME_V3.ps1"
$Runtime = Join-Path $env:TEMP "REPAIR_TRADINGCORE_RUNTIME_V3_RUNTIME.ps1"

Write-Host ""
Write-Host "=== TRADINGCORE RUNTIME REPAIR V3A BOOTSTRAP ===" -ForegroundColor Cyan

if (-not (Test-Path $Source)) {
    Write-Host "V3 source missing - nothing changed." -ForegroundColor Red
    exit 1
}

$Text = Get-Content $Source -Raw
$Patched = $Text.Replace('"$Name:MISSING"','"${Name}:MISSING"')
$Patched = $Patched.Replace('"$Name:$($T.State)"','"${Name}:$($T.State)"')

if ($Patched -eq $Text) {
    # It may already have been corrected. That is also safe.
    Write-Host "Interpolation patch already present or not required." -ForegroundColor DarkGray
}

Set-Content $Runtime -Value $Patched -Encoding UTF8

$Tokens = $null
$Errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $Runtime,
    [ref]$Tokens,
    [ref]$Errors
)

if ($Errors.Count -gt 0) {
    Write-Host "V3 PARSER CHECK FAILED - NOTHING CHANGED" -ForegroundColor Red
    $Errors | ForEach-Object { Write-Host $_.Message -ForegroundColor Yellow }
    exit 1
}

Write-Host "V3 parser check: PASS" -ForegroundColor Green
Write-Host "Starting unified runtime repair..." -ForegroundColor Cyan

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Runtime
exit $LASTEXITCODE
