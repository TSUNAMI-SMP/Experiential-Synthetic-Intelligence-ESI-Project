@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-esi02-windows.ps1"
if errorlevel 1 (
  echo ESI-02 startup failed. Read the message above.
  pause
  exit /b 1
)
