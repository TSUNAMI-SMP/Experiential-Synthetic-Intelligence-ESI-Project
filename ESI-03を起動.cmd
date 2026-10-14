@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-esi03-windows.ps1"
if errorlevel 1 (
  echo ESI-03 startup failed. Read the message above.
  pause
  exit /b 1
)
