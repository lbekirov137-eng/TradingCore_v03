#requires -Version 5.1
<# One-command independent confirmation for the frozen BTC 1H candidate. #>
$ErrorActionPreference="Stop"
$Repo="C:\TradingCore_Cloud_Recovery_20260814_135929"
$Py="C:\TradingCore_Collector_B\.venv\Scripts\python.exe"
function Fail([string]$Text){Write-Host "";Write-Host "BTC 1H CONFIRMATORY STOPPED SAFELY" -ForegroundColor Red;Write-Host $Text -ForegroundColor Yellow;Write-Host "Existing TradingCore services continue. LIVE / real orders DISABLED." -ForegroundColor Green;exit 1}
Write-Host "";Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host " BTC 1H FROZEN CANDIDATE - INDEPENDENT BYBIT CONFIRMATION" -ForegroundColor Cyan;Write-Host "================================================================================" -ForegroundColor Cyan;Write-Host "No new strategy. No tuning. Reuses cached Bybit history. No terminal cleanup."
if(-not(Test-Path $Repo)){Fail "Repo missing"};if(-not(Test-Path $Py)){Fail "Python missing"}
$env:TRADING_ENVIRONMENT="PAPER";$env:LIVE_TRADING="false";$env:PAPER_TRADING="true";$env:DEMO_ONLY="true"
@("BINANCE_API_KEY","BINANCE_SECRET","BINANCE_SECRET_KEY","BYBIT_API_KEY","BYBIT_SECRET","BYBIT_SECRET_KEY","OKX_API_KEY","OKX_SECRET","OPENAI_API_KEY")|%{Remove-Item "Env:$_" -ErrorAction SilentlyContinue}
Push-Location $Repo
try{
  & $Py -m py_compile .\btc_1h_bybit_confirmatory.py .\btc_1h_bybit_confirmatory_selftest.py
  if($LASTEXITCODE -ne 0){Fail "Compile failed"}
  & $Py .\btc_1h_bybit_confirmatory_selftest.py
  if($LASTEXITCODE -ne 0){Fail "Self-test failed"}
  & $Py .\btc_1h_bybit_confirmatory.py
  if($LASTEXITCODE -ne 0){Fail "Confirmatory engine returned non-zero"}
}finally{Pop-Location}
Write-Host "";Write-Host "Confirmatory run complete. Existing BTC 1H Forward Shadow remains running." -ForegroundColor Green;Write-Host "LIVE / real orders: DISABLED" -ForegroundColor Green
