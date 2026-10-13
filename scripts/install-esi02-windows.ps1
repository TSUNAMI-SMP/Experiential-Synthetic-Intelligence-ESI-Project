$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$EsiRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $EsiRoot
$EsiEnvironment = Join-Path $EsiRoot ".venv-esi02"
$EsiPython = Join-Path $EsiEnvironment "Scripts\python.exe"
if (![Environment]::Is64BitOperatingSystem) { throw "ESI-02には64ビットのWindowsが必要です。" }
if (!(Test-Path -LiteralPath $EsiPython)) {
    if (!(Get-Command py -ErrorAction SilentlyContinue)) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "Python 3.12が必要です。公式ページから64ビット版を導入して、ESI-02をもう一度開いてください。ブラウザーで公式ページを開きます。",
            "ESI-02の初回準備") | Out-Null
        Start-Process "https://www.python.org/downloads/release/python-31210/"
        throw "Python 3.12の導入後に、このファイルをもう一度開いてください。"
    }
    & py -3.12 -c "import struct; assert struct.calcsize('P') == 8"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12の64ビット版を導入してください。" }
    & py -3.12 -m venv $EsiEnvironment
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12（64ビット版）を導入してください。" }
}
Write-Output "ESI-02の画面・聞き取り・音声のライブラリを準備しています…"
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
& $EsiPython -c "import sys, httpx, numpy, pydantic, sounddevice, live2d; from PySide6.QtWidgets import QApplication; from esi02.config import Settings; assert sys.version_info[:2] == (3, 12); assert sys.maxsize > 2**32; print('ESI-02 libraries imported successfully')"
if ($LASTEXITCODE -ne 0) { throw "ライブラリの読み込みを確認できませんでした。" }
Write-Output "ESI-02の初回準備が終わりました。『ESI-02を起動.cmd』をダブルクリックしてください。AIデータは起動画面から準備できます。"
