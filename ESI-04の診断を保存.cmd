@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\diagnose-esi04-windows.ps1"
if errorlevel 1 (
  echo ESI-04 diagnostics failed. Read the message above.
  pause
  exit /b 1
)
echo Diagnostic report saved in data\esi04\diagnostics.
pause
