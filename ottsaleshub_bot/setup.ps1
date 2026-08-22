# One-time setup for Windows PowerShell.  Run:  .\setup.ps1
$ErrorActionPreference = "Stop"

Write-Host "`n[1/4] Checking files..." -ForegroundColor Cyan
$required = @('bot.py','config.py','db.py','delivery.py','flair.py','handlers_admin.py',
              'handlers_user.py','keyboards.py','payments.py','watcher.py','webapp.py',
              'webhook.py','requirements.txt','static\app.css','static\tg.js',
              'static\admin.html','static\shop.html')
$missing = $required | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Write-Host "Missing files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
Write-Host "  all present" -ForegroundColor Green

Write-Host "`n[2/4] Creating virtual environment..." -ForegroundColor Cyan
if (-not (Test-Path .venv)) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
$ver = & .\.venv\Scripts\python.exe -c "import aiogram; print(aiogram.__version__)"
Write-Host "  aiogram $ver" -ForegroundColor Green

Write-Host "`n[3/4] Preparing config..." -ForegroundColor Cyan
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "  created .env from the template" -ForegroundColor Green
} else {
    Write-Host "  .env already exists, leaving it alone" -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force -Path C:\shopbot | Out-Null

Write-Host "`n[4/4] Checking your settings..." -ForegroundColor Cyan
$env_text = Get-Content .env -Raw
if ($env_text -match 'BOT_TOKEN=123456:ABC') {
    Write-Host "  BOT_TOKEN is still the placeholder." -ForegroundColor Yellow
    Write-Host "  Opening .env - set BOT_TOKEN and ADMIN_IDS, save, then close Notepad.`n"
    Start-Process notepad .env -Wait
}

Write-Host "`nSetup done. Start the bot with:" -ForegroundColor Green
Write-Host "    .\run.ps1`n" -ForegroundColor White
