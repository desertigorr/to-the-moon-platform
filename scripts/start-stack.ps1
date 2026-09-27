param([switch]$NoBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop with Linux containers first.' }
    $configText = & docker compose config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read Compose configuration.' }
    $config = $configText | ConvertFrom-Json
    if ($config.services.backend.environment.DATA_MODE -eq 'replay') {
        throw 'DATA_MODE=replay has been removed. Set DATA_MODE=emulator_replay to use the official emulator.'
    }
    $imageId = & docker images --quiet ndtp-telemetry-emulator:1.0
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list Docker images.' }
    if (-not $imageId) {
        $archive = Join-Path $projectRoot 'dataset\ndtp-telemetry-emulator.tar'
        if (-not (Test-Path -LiteralPath $archive)) {
            throw 'Copy the official organizer archive to dataset/ndtp-telemetry-emulator.tar, then run this script again. The third-party image is not redistributed in Git.'
        }
        & docker load -i $archive
        if ($LASTEXITCODE -ne 0) { throw 'Could not load the official emulator image.' }
    }
    $composeArgs = @('compose', 'up', '-d', '--remove-orphans', '--wait', '--wait-timeout', '180')
    if (-not $NoBuild) { $composeArgs += '--build' }
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Stack startup failed. Check docker compose logs and ports 8000/8080/9201/18080. Another process may occupy a port.'
    }
    $apiPort = $config.services.backend.ports | Where-Object { $_.target -eq 8000 } | Select-Object -ExpandProperty published
    if ($config.services.backend.environment.DATA_MODE -in @('emulator', 'emulator_replay')) {
        $deadline = (Get-Date).AddSeconds(60)
        do {
            try { $health = Invoke-RestMethod "http://localhost:$apiPort/api/health" -TimeoutSec 3 }
            catch { $health = $null }
            $hasPackets = if ($health.source.mode -eq 'emulator_replay') { $health.emulator.accepted_measurements -gt 0 } else { $health.received_packets -gt 0 }
            if ($health.emulator.status -eq 'ready' -and $hasPackets) { break }
            Start-Sleep -Seconds 2
        } while ((Get-Date) -lt $deadline)
        if ($health.emulator.status -ne 'ready' -or -not $hasPackets) {
            throw 'Containers started but official NDTP telemetry did not arrive. See docker compose logs backend emulator.'
        }
        Write-Host "Official emulator: $($health.emulator.configured_units) configured devices; $($health.vehicles) observed; $($health.received_packets) NDTP packets received."
    }
    $frontendPort = $config.services.frontend.ports[0].published
    Write-Host "Dashboard: http://localhost:$frontendPort"
    Write-Host 'Status: docker compose ps; logs: docker compose logs -f backend ml emulator'
} finally { Pop-Location }
