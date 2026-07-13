# Thin launcher for lux_trader in the Quant conda environment.
# --no-capture-output keeps the interactive terminal UI (dashboard/compact) working.
#
# Usage:
#   .\scripts\lux.ps1 live-dry-run --config configs/live.example.toml --reset-store
#   .\scripts\lux.ps1 live-execute --config configs/config.live.exec.local.toml --reset-store
#   .\scripts\lux.ps1 replay --config configs/replay.fixture.toml --reset-store

$ErrorActionPreference = 'Stop'
$conda = 'D:\Users\miniconda3\condabin\conda.bat'
$projectRoot = Split-Path -Parent $PSScriptRoot
$liveExecuteEnvGates = @(
    'LUX_READONLY_BROKER',
    'PROJECT_LUX_ALLOW_LIVE_ORDER',
    'FUBON_ALLOW_LIVE_ORDER',
    'BINANCE_ALLOW_LIVE_ORDER'
)
$restoreEnv = @{}
$autoLiveExecuteEnv = $args.Count -gt 0 -and $args[0] -eq 'live-execute'

Push-Location $projectRoot
try {
    if ($autoLiveExecuteEnv) {
        foreach ($name in $liveExecuteEnvGates) {
            $restoreEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, '1', 'Process')
        }
    }
    & $conda run -n Quant --no-capture-output python -m lux_trader @args
    exit $LASTEXITCODE
}
finally {
    if ($autoLiveExecuteEnv) {
        foreach ($name in $liveExecuteEnvGates) {
            [Environment]::SetEnvironmentVariable($name, $restoreEnv[$name], 'Process')
        }
    }
    Pop-Location
}
