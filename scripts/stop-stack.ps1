$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & docker compose down --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw 'Cannot stop Compose services.' }
} finally { Pop-Location }
