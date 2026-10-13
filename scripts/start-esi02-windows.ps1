$ErrorActionPreference = "Stop"
$EsiRoot = Split-Path $PSScriptRoot -Parent
$EsiPython = Join-Path $EsiRoot ".venv-esi02\Scripts\pythonw.exe"
Set-Location -LiteralPath $EsiRoot
if (!(Test-Path -LiteralPath $EsiPython)) {
    & (Join-Path $PSScriptRoot "install-esi02-windows.ps1")
    if ($LASTEXITCODE -ne 0) { throw "ESI-02の初回準備を完了できませんでした。" }
}
if (!(Test-Path -LiteralPath $EsiPython)) { throw "ESI-02用のPythonが見つかりません。" }
$env:PYTHONPATH = Join-Path $EsiRoot "src"
Start-Process -FilePath $EsiPython -WorkingDirectory $EsiRoot -ArgumentList @("-m", "esi02.launcher")
