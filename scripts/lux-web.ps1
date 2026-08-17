# Launcher for the read-only web viewer, on its own so .claude/launch.json can
# reference a path inside the repo instead of a hardcoded interpreter.
#
# The viewer opens the store with mode=ro and never writes, so running it
# against a live store is safe. It sets none of the live-order env gates, and
# starting it can never put an order on the wire.
#
# Usage:
#   .\scripts\lux-web.ps1 --config configs/config.live.ccf_umc.execute.local.toml --port 8787

$ErrorActionPreference = 'Stop'
$conda = & (Join-Path $PSScriptRoot 'find-conda.ps1')
Set-Location (Split-Path -Parent $PSScriptRoot)
& $conda run -n Quant --no-capture-output python -m lux_trader.web @args
exit $LASTEXITCODE
