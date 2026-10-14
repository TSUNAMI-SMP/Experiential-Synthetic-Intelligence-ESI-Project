$ErrorActionPreference = "Stop"
$EsiRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $EsiRoot
$EsiPython = Join-Path $EsiRoot ".venv-esi03\Scripts\python.exe"
if (!(Test-Path -LiteralPath $EsiPython)) { $EsiPython = Join-Path $EsiRoot ".venv-esi02\Scripts\python.exe" }
$EsiLogs = Join-Path $EsiRoot "data\esi03\logs"
New-Item -ItemType Directory -Force -Path $EsiLogs | Out-Null
$EsiDiagnosticStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
Start-Transcript -Path (Join-Path $EsiLogs "diagnostic-$EsiDiagnosticStamp.log") | Out-Null
try {
    if (!(Test-Path -LiteralPath $EsiPython)) {
        throw "ESI-03のPythonがありません。『ESI-03の初回準備・更新.cmd』を先に実行してください。"
    }
    $env:PYTHONPATH = Join-Path $EsiRoot "src"
    $EsiSavePath = Join-Path $EsiRoot "data\esi03\session.sqlite3"
    $EsiLauncherFile = Join-Path $EsiRoot "data\esi03\launcher.json"
    if (Test-Path -LiteralPath $EsiLauncherFile) {
        try {
            $EsiLauncherPreferences = Get-Content -LiteralPath $EsiLauncherFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($EsiLauncherPreferences.save_path -is [string] -and $EsiLauncherPreferences.save_path) {
                $EsiSavePath = $EsiLauncherPreferences.save_path
            }
        } catch {
            Write-Warning "起動設定を読めません。元ファイルは保持し、既定の保存先を診断します。"
        }
    }
    $EsiReportFolder = Join-Path $EsiRoot "data\esi03\diagnostics"
    New-Item -ItemType Directory -Force -Path $EsiReportFolder | Out-Null
    $EsiReport = Join-Path $EsiReportFolder "diagnostic-$EsiDiagnosticStamp.json"
    $EsiArguments = @("-m", "esi03", "--database", $EsiSavePath)
    if (!(Test-Path -LiteralPath $EsiSavePath)) {
        $EsiArguments += @("--config", (Join-Path $EsiRoot "config\esi03-windows.toml"))
    }
    $EsiArguments += @("doctor", "--output", $EsiReport)
    & $EsiPython @EsiArguments
    if ($LASTEXITCODE -ne 0) { throw "診断を実行できませんでした。この窓の表示とlogsの記録を確認してください。" }
    Write-Output "診断ファイル：$EsiReport"
} finally {
    Stop-Transcript | Out-Null
}
