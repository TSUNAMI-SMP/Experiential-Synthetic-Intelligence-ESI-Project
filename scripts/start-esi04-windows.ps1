$ErrorActionPreference = "Stop"
$EsiRoot = Split-Path $PSScriptRoot -Parent
$EsiPython = Join-Path $EsiRoot ".venv-esi04\Scripts\pythonw.exe"
if (!(Test-Path -LiteralPath $EsiPython)) { $EsiPython = Join-Path $EsiRoot ".venv-esi03\Scripts\pythonw.exe" }
if (!(Test-Path -LiteralPath $EsiPython)) { $EsiPython = Join-Path $EsiRoot ".venv-esi02\Scripts\pythonw.exe" }
Set-Location -LiteralPath $EsiRoot
$EsiLogs = Join-Path $EsiRoot "data\esi04\logs"
New-Item -ItemType Directory -Force -Path $EsiLogs | Out-Null
$EsiRunStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
if (!(Test-Path -LiteralPath $EsiPython)) {
    & (Join-Path $PSScriptRoot "install-esi04-windows.ps1")
    if ($LASTEXITCODE -ne 0) { throw "ESI-04の初回準備を完了できませんでした。" }
    $EsiPython = Join-Path $EsiRoot ".venv-esi04\Scripts\pythonw.exe"
    if (!(Test-Path -LiteralPath $EsiPython)) { $EsiPython = Join-Path $EsiRoot ".venv-esi03\Scripts\pythonw.exe" }
if (!(Test-Path -LiteralPath $EsiPython)) { $EsiPython = Join-Path $EsiRoot ".venv-esi02\Scripts\pythonw.exe" }
}
if (!(Test-Path -LiteralPath $EsiPython)) { throw "ESI-04用のPythonが見つかりません。" }
$env:PYTHONPATH = Join-Path $EsiRoot "src"
Start-Process -FilePath $EsiPython -WorkingDirectory $EsiRoot -ArgumentList @("-m", "esi04.bootstrap") `
    -RedirectStandardOutput (Join-Path $EsiLogs "startup-$EsiRunStamp-stdout.log") `
    -RedirectStandardError (Join-Path $EsiLogs "startup-$EsiRunStamp-stderr.log")
