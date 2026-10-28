$ErrorActionPreference = "Stop"

$Career = Split-Path -Parent $PSScriptRoot
Set-Location $Career

Write-Host "Starting search-only Career Agent Telegram bot..."
Write-Host ""
$ExistingBot = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match '[t]elegram_career_agent\.py'
}
if ($ExistingBot) {
    Write-Host "Career Agent Telegram bot is already running."
    exit 0
}

Write-Host "Stopping any Hermes Gateway Telegram poller to avoid conflicts..."
$GatewayProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'hermes_cli\.main\s+gateway\s+run'
}
foreach ($GatewayProcess in $GatewayProcesses) {
    Stop-Process -Id $GatewayProcess.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host ""
Write-Host "Telegram commands:"
Write-Host "  HELP"
Write-Host "  SEARCH"
Write-Host "  PROVIDERS"
Write-Host "  WATCH START"
Write-Host "  WATCH STOP"
Write-Host "  WATCH STATUS"
Write-Host "  WATCH NOW"
Write-Host "  LATEST (sends all qualified unseen jobs; TOP10, TOP5, and DIGEST are aliases)"
Write-Host "  STATUS"
Write-Host "  STOP AGENT"
Write-Host ""
Write-Host "Keep this PowerShell window open while using Telegram commands."
Write-Host ""

$VenvPython = Join-Path $Career ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }
& $Python .\scripts\telegram_career_agent.py
