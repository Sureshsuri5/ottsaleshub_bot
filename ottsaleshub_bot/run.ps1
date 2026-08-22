# Starts the bot using the project's virtual environment.
if (-not (Test-Path .venv)) { Write-Host "Run .\setup.ps1 first." -ForegroundColor Red; exit 1 }
if (-not (Test-Path .env))  { Write-Host "No .env - run .\setup.ps1 first." -ForegroundColor Red; exit 1 }
& .\.venv\Scripts\python.exe bot.py
