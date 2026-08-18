# Locates conda for the launchers, and prints a path to a real executable.
#
# Split out of lux.ps1 so lux-web.ps1 cannot drift from it. A stale copy of the
# fallback list is the failure this prevents: conda is routinely absent from
# PATH on this machine while conda.bat sits under %USERPROFILE%, so the
# fallback IS the working path here, not a rare backstop.
#
# Usage:
#   $conda = & (Join-Path $PSScriptRoot 'find-conda.ps1')

$ErrorActionPreference = 'Stop'

# Only an Application has a file path in .Source. `conda init powershell`
# installs conda as a FUNCTION from the Conda module, and a FunctionInfo's
# .Source is the MODULE NAME -- so this returned the literal string "Conda".
# `& "Conda" run ...` still worked, because the call operator resolves it back
# to the function, which is why live kept starting; Start-Process -FilePath
# needs a real executable and failed with "the system cannot find the file
# specified". Measured 2026-08-17 and again 2026-08-18, the web viewer both
# times, while the trading process was unaffected.
$condaCommand = Get-Command conda -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($condaCommand -and (Test-Path -LiteralPath $condaCommand.Source)) {
    return $condaCommand.Source
}

$candidates = @(
    (Join-Path $env:USERPROFILE 'anaconda3\condabin\conda.bat'),
    (Join-Path $env:USERPROFILE 'miniconda3\condabin\conda.bat'),
    'D:\Users\miniconda3\condabin\conda.bat'
)
$found = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $found) {
    throw 'Unable to find conda. Add conda to PATH or install Anaconda/Miniconda.'
}
return $found
