[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $Python = Get-Command python -ErrorAction Stop
    & $Python.Source -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install --upgrade pip
Push-Location $Root
try {
    & $VenvPython -m pip install -e ".[dev,build]"
}
finally {
    Pop-Location
}

"Preparação concluída. Executa .\scripts\run.ps1"
