# Thin launcher for lux_trader in the Quant conda environment.
# --no-capture-output keeps the interactive terminal UI (dashboard/compact) working.
#
# Usage:
#   .\scripts\lux.ps1 live --mode dry-run --config configs/live.example.toml --reset-store
#   .\scripts\lux.ps1 live --mode execute --config configs/config.live.ccf_umc.execute.local.toml --reset-store
#   .\scripts\lux.ps1 replay --config configs/replay.fixture.ccf_umc.toml --reset-store
#
# `live` also brings up the read-only web viewer and shuts it down afterwards.
# Environment knobs (no new script parameters, so @args passing is untouched):
#   LUX_NO_WEB=1      do not start the viewer
#   LUX_WEB_PORT      default 8787
#   LUX_WEB_HOST      default 127.0.0.1; anything else turns on token auth
#   LUX_WEB_TOKEN     supply your own token instead of a generated one

$ErrorActionPreference = 'Stop'
$conda = & (Join-Path $PSScriptRoot 'find-conda.ps1')
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

$configIndex = [Array]::IndexOf([string[]]$args, '--config')
$configPath = if ($configIndex -ge 0 -and $configIndex + 1 -lt $args.Count) {
    $args[$configIndex + 1]
}
else { '' }
$webProcess = $null

Push-Location $projectRoot
try {
    # The viewer is a SEPARATE process, deliberately. It must have no way to
    # reach the loop that places real orders: if it throws, fails to bind, or
    # hangs, trading is untouched. It is also started BEFORE the live-order env
    # gates below, so it never inherits them.
    if ($command -eq 'live' -and $configPath -and $env:LUX_NO_WEB -ne '1') {
        $webPort = if ($env:LUX_WEB_PORT) { $env:LUX_WEB_PORT } else { '8787' }
        $webHost = if ($env:LUX_WEB_HOST) { $env:LUX_WEB_HOST } else { '127.0.0.1' }
        $webToken = $env:LUX_WEB_TOKEN
        if (-not $webToken -and $webHost -ne '127.0.0.1' -and $webHost -ne 'localhost') {
            # Generated here rather than in the server so the URL is printed in
            # THIS terminal -- the server's own output is hidden.
            $webToken = [Guid]::NewGuid().ToString('N')
        }
        try {
            $busy = Get-NetTCPConnection -State Listen -LocalPort $webPort -ErrorAction SilentlyContinue
            if ($busy) {
                Write-Host "web viewer: port $webPort already in use; leaving it alone"
            }
            else {
                $webArgs = @(
                    'run', '-n', 'Quant', '--no-capture-output',
                    'python', '-m', 'lux_trader.web',
                    '--config', $configPath, '--host', $webHost, '--port', $webPort
                )
                if ($webToken) { $webArgs += @('--token', $webToken) }
                $webProcess = Start-Process -FilePath $conda -ArgumentList $webArgs -PassThru -WindowStyle Hidden
                $shown = if ($webHost -eq '0.0.0.0') { '127.0.0.1' } else { $webHost }
                $query = if ($webToken) { "/?token=$webToken" } else { '' }
                Write-Host "web viewer (read-only): http://${shown}:${webPort}${query}"
                if ($webToken) {
                    Write-Host 'web viewer: plain HTTP, no TLS -- keep it on a VPN/LAN, not the open internet.'
                }
            }
        }
        catch {
            # A viewer that will not start is never a reason to not start trading.
            Write-Host "web viewer failed to start; continuing without it: $($_.Exception.Message)"
        }
    }

    foreach ($name in $autoEnvGates) {
        $restoreEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, '1', 'Process')
    }
    & $conda run -n Quant --no-capture-output python -m lux_trader @args
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $webProcess) {
        # /T because conda run wraps the python child; killing only the wrapper
        # would leave the port held.
        try {
            Start-Process -FilePath 'taskkill.exe' `
                -ArgumentList @('/PID', $webProcess.Id, '/T', '/F') `
                -WindowStyle Hidden -Wait
        }
        catch { }
    }
    foreach ($name in $autoEnvGates) {
        [Environment]::SetEnvironmentVariable($name, $restoreEnv[$name], 'Process')
    }
    Pop-Location
}
