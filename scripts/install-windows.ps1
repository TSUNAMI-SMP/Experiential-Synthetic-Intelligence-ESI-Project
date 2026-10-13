$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required" }
}
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; do not disable hash checks" }
& .\.venv\Scripts\python.exe -m pip install --require-hashes --no-deps -r requirements-live2d.lock
if ($LASTEXITCODE -ne 0) { throw "Live2D renderer installation failed" }
& .\.venv\Scripts\python.exe -m pip install --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw "ESI installation failed" }
& .\.venv\Scripts\python.exe -m pytest -q -m "not integration"
if ($LASTEXITCODE -ne 0) { throw "ESI tests failed" }
Write-Output "ESI-01 Python environment prepared. Install local models and engines using docs/windows.md."
