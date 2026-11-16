@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
"python\python.exe" -B scripts\windows_launcher.py stop
if errorlevel 1 pause
