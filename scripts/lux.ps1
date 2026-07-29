# Thin launcher for lux_trader in the Quant conda environment.
# --no-capture-output keeps the interactive terminal UI (dashboard/compact) working.
#
# Usage:
#   .\scripts\lux.ps1 live --mode dry-run --config configs/live.example.toml --reset-store
#   .\scripts\lux.ps1 live --mode execute --config configs/config.live.exec.local.toml --reset-store
#   .\scripts\lux.ps1 replay --config configs/replay.fixture.toml --reset-store

$ErrorActionPreference = 'Stop'
$condaCommand = Get-Command conda -ErrorAction SilentlyContinue
$conda = if ($condaCommand) {
    $condaCommand.Source
}
else {
    $candidates = @(
        (Join-Path $env:USERPROFILE 'anaconda3\condabin\conda.bat'),
        (Join-Path $env:USERPROFILE 'miniconda3\condabin\conda.bat'),
        'D:\Users\miniconda3\condabin\conda.bat'
    )
    $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $conda) {
    throw 'Unable to find conda. Add conda to PATH or install Anaconda/Miniconda.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$liveExecuteEnvGates = @(
    'LUX_READONLY_BROKER',
    'PROJECT_LUX_ALLOW_LIVE_ORDER',
    'FUBON_ALLOW_LIVE_ORDER',
    'IBKR_ALLOW_LIVE_ORDER'
)
$restoreEnv = @{}
# The gates depend on `live --mode`, so read the mode rather than the verb.
$command = if ($args.Count -gt 0) { $args[0] } else { '' }
$modeIndex = [Array]::IndexOf([string[]]$args, '--mode')
$mode = if ($modeIndex -ge 0 -and $modeIndex + 1 -lt $args.Count) { $args[$modeIndex + 1] } else { '' }
$autoEnvGates = if ($command -eq 'live' -and $mode -eq 'execute') {
    $liveExecuteEnvGates
}
elseif ($command -eq 'live' -and $mode -eq 'dry-run') {
    @('LUX_READONLY_BROKER')
}
else {
    @()
}

Push-Location $projectRoot
try {
    foreach ($name in $autoEnvGates) {
        $restoreEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, '1', 'Process')
    }
    & $conda run -n Quant --no-capture-output python -m lux_trader @args
    exit $LASTEXITCODE
}
finally {
    foreach ($name in $autoEnvGates) {
        [Environment]::SetEnvironmentVariable($name, $restoreEnv[$name], 'Process')
    }
    Pop-Location
}
