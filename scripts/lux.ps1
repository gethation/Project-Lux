param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $LuxArgs
)

$ErrorActionPreference = "Stop"

$CondaBat = "D:\Users\miniconda3\condabin\conda.bat"
if (-not (Test-Path -LiteralPath $CondaBat)) {
    throw "Conda launcher not found: $CondaBat"
}

& $CondaBat run --no-capture-output -n Quant python -m lux_trader @LuxArgs
exit $LASTEXITCODE
