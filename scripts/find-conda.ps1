# Locates conda for the launchers, and prints its path.
#
# Split out of lux.ps1 so lux-web.ps1 cannot drift from it. A stale copy of the
# fallback list is the failure this prevents: conda is routinely absent from
# PATH on this machine while conda.bat sits under %USERPROFILE%, so the
# fallback IS the working path here, not a rare backstop.
#
# Usage:
#   $conda = & (Join-Path $PSScriptRoot 'find-conda.ps1')

$ErrorActionPreference = 'Stop'

$condaCommand = Get-Command conda -ErrorAction SilentlyContinue
if ($condaCommand) {
    return $condaCommand.Source
}

$candidates = @(
    (Join-Path $env:USERPROFILE 'anaconda3\condabin\conda.bat'),
    (Join-Path $env:USERPROFILE 'miniconda3\condabin\conda.bat'),
    'D:\Users\miniconda3\condabin\conda.bat'
)
$found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $found) {
    throw 'Unable to find conda. Add conda to PATH or install Anaconda/Miniconda.'
}
return $found
