@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1"
if errorlevel 1 (
  echo Setup failed. Keep this window open to check the message.
  pause
  exit /b 1
)
echo ESI-01 setup finished. Double-click the ESI-01 launcher.
pause
