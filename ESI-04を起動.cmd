@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-esi04-windows.ps1"
if errorlevel 1 (
  echo ESI-04 startup failed. Read the message above.
  pause
  exit /b 1
)
