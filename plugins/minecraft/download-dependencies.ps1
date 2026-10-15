param()
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'dependencies.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$destination = Join-Path $PSScriptRoot 'release\mods'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
foreach ($entry in $manifest.files) {
    $target = Join-Path $destination $entry.filename
    $valid = (Test-Path -LiteralPath $target) -and ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq $entry.sha256)
    if (-not $valid) {
        Write-Host ('Downloading: ' + $entry.name)
        $temporary = $target + '.partial'
        Invoke-WebRequest -Uri $entry.url -OutFile $temporary -UseBasicParsing
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $entry.sha256) {
            Remove-Item -LiteralPath $temporary -Force
            throw ('SHA256 mismatch: ' + $entry.filename)
        }
        Move-Item -LiteralPath $temporary -Destination $target -Force
    }
    Write-Host ('Verified: ' + $entry.filename)
}
$bridge = Join-Path $PSScriptRoot 'release\esi-minecraft-bridge-0.4.4.jar'
if (-not (Test-Path -LiteralPath $bridge)) { throw 'ESI Bridge JAR is missing. Download the complete ESI-04 ZIP.' }
Copy-Item -LiteralPath $bridge -Destination $destination -Force
Write-Host ('Ready. Copy these three JAR files to your Fabric 1.21.11 mods folder: ' + $destination)
Start-Process explorer.exe -ArgumentList ('"' + $destination + '"')
