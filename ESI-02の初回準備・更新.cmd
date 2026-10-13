@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-esi02-windows.ps1"
if errorlevel 1 (
  echo ESI-02 preparation failed. Read the message above.
  pause
  exit /b 1
)
echo ESI-02 Python preparation finished. Double-click the ESI-02 launcher.
pause
