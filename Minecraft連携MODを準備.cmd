@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0plugins\minecraft\download-dependencies.ps1"
if errorlevel 1 echo Failed. See the error above.
pause
