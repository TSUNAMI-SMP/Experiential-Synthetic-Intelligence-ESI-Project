@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\diagnose-esi02-windows.ps1"
if errorlevel 1 (
  echo ESI-02 diagnostics failed. Read the message above.
  pause
  exit /b 1
)
echo Diagnostic report saved in data\esi02\diagnostics.
pause
