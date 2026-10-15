$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$EsiRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $EsiRoot
$EsiLogs = Join-Path $EsiRoot "data\esi04\logs"
New-Item -ItemType Directory -Force -Path $EsiLogs | Out-Null
$EsiSetupLog = Join-Path $EsiLogs ("install-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff") + ".log")
Start-Transcript -Path $EsiSetupLog | Out-Null
try {
$EsiEnvironment = Join-Path $EsiRoot ".venv-esi04"
$EsiPreviousEnvironment = Join-Path $EsiRoot ".venv-esi03"
if (!(Test-Path -LiteralPath (Join-Path $EsiPreviousEnvironment "Scripts\python.exe"))) { $EsiPreviousEnvironment = Join-Path $EsiRoot ".venv-esi02" }
if (!(Test-Path -LiteralPath (Join-Path $EsiEnvironment "Scripts\python.exe")) -and (Test-Path -LiteralPath (Join-Path $EsiPreviousEnvironment "Scripts\python.exe"))) { $EsiEnvironment = $EsiPreviousEnvironment }
$EsiPython = Join-Path $EsiEnvironment "Scripts\python.exe"
if (![Environment]::Is64BitOperatingSystem) { throw "ESI-04には64ビットのWindowsが必要です。" }
if (!(Test-Path -LiteralPath $EsiPython)) {
    if (!(Get-Command py -ErrorAction SilentlyContinue)) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "Python 3.12が必要です。公式ページから64ビット版を導入して、ESI-04をもう一度開いてください。ブラウザーで公式ページを開きます。",
            "ESI-04の初回準備") | Out-Null
        Start-Process "https://www.python.org/downloads/release/python-31210/"
        throw "Python 3.12の導入後に、このファイルをもう一度開いてください。"
    }
    & py -3.12 -c "import struct; assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12の64ビット版を導入してください。" }
    & py -3.12 -m venv $EsiEnvironment
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12（64ビット版）を導入してください。" }
}
Write-Output "ESI-04の画面・聞き取り・音声のライブラリを準備しています…"
& $EsiPython -m pip install --require-hashes --only-binary=:all: -r (Join-Path $EsiRoot "requirements.lock")
if ($LASTEXITCODE -ne 0) { throw "ライブラリの導入に失敗しました。通信状態と表示された記録を確認してください。" }
& $EsiPython -m pip install --require-hashes --only-binary=:all: --no-deps -r (Join-Path $EsiRoot "requirements-live2d.lock")
if ($LASTEXITCODE -ne 0) { throw "Live2D表示ライブラリの導入に失敗しました。" }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $EsiGpu = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null
    if ($LASTEXITCODE -eq 0 -and $EsiGpu) {
        Write-Output "NVIDIA GPU用の聞き取りライブラリを準備しています…"
        & $EsiPython -m pip install --require-hashes --only-binary=:all: -r (Join-Path $EsiRoot "requirements-gpu-windows.lock")
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "GPU用の聞き取りライブラリを導入できませんでした。auto設定ではCPUへ切り替え、理由を画面に表示します。"
        }
    }
}
$env:PYTHONPATH = Join-Path $EsiRoot "src"
& $EsiPython -c "import sys, httpx, numpy, pydantic, sounddevice, live2d; from PySide6.QtWidgets import QApplication; from esi04.config import Settings; assert sys.version_info[:2] == (3, 12); assert sys.maxsize > 2**32; print('ESI-04 libraries imported successfully')"
if ($LASTEXITCODE -ne 0) { throw "ライブラリの読み込みを確認できませんでした。" }
Write-Output "ESI-04の初回準備が終わりました。『ESI-04を起動.cmd』をダブルクリックしてください。AIデータは起動画面から準備できます。"
} finally {
    Stop-Transcript | Out-Null
    Write-Output "導入の記録：$EsiSetupLog"
}
