$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$nodeFolder = Join-Path $root 'node_modules\node\bin'
if (-not (Test-Path (Join-Path $nodeFolder 'node.exe'))) {
    throw 'Project-local Node 24 is missing. Run npm install in the project directory.'
}
$env:PATH = "$nodeFolder;$env:PATH"
Push-Location $root
try {
    & (Join-Path $root 'node_modules\.bin\rayfin.cmd') @args
    if ($LASTEXITCODE -ne 0) { throw "Rayfin exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
