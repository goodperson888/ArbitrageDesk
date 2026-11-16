@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
if not exist "python\python.exe" (
  echo Download the Windows portable build from GitHub Actions first.
  pause
  exit /b 1
)
"python\python.exe" -B scripts\windows_launcher.py start
if errorlevel 1 pause
