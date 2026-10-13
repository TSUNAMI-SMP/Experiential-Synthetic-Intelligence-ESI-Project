$ErrorActionPreference = "Stop"
$EsiRoot = Split-Path $PSScriptRoot -Parent
$EsiPython = Join-Path $EsiRoot ".venv\Scripts\pythonw.exe"
if (!(Test-Path $EsiPython)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "ESI-01の初回準備がまだ終わっていません。docsフォルダーの『はじめての導入.md』にある準備を行ってください。",
        "ESI-01の準備")
    exit 1
}
Start-Process -FilePath $EsiPython -WorkingDirectory $EsiRoot -ArgumentList @("-m", "esi.desktop")
