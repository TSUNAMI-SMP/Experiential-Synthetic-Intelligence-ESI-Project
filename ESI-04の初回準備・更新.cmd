@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-esi04-windows.ps1"
if errorlevel 1 (
  echo ESI-04 preparation failed. Read the message above.
  pause
  exit /b 1
)
echo ESI-04 Python preparation finished. Double-click the ESI-04 launcher.
pause
