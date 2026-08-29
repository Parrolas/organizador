[CmdletBinding()]
param(
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Ambiente não encontrado. Executa primeiro .\scripts\setup.ps1"
}

$Arguments = @("-m", "organizador.main")
if ($Background) {
    $Arguments += "--background"
}

& $Python @Arguments
exit $LASTEXITCODE
