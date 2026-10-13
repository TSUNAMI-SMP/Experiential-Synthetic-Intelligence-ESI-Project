@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m esi.desktop
