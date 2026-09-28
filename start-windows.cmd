@echo off
setlocal
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Fill BOT_TOKEN, ADMIN_ID and DATABASE_URL, then run this file again.
  notepad .env
  goto end
)
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv
  if errorlevel 1 goto end
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto end
.venv\Scripts\python.exe -m alembic upgrade head
if errorlevel 1 goto end
.venv\Scripts\python.exe -m app.main
:end
pause
