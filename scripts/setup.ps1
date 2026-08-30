[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Constraints = Join-Path $Root "constraints-release.txt"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $Python = Get-Command python -ErrorAction Stop
    & $Python.Source -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Não foi possível criar o ambiente virtual" }
}

& $VenvPython -m pip install --upgrade "pip==26.2.1"
if ($LASTEXITCODE -ne 0) { throw "Não foi possível instalar a versão fixada do pip" }
$PreviousConstraint = $env:PIP_CONSTRAINT
$env:PIP_CONSTRAINT = ([System.Uri]$Constraints).AbsoluteUri
Push-Location $Root
try {
    & $VenvPython -m pip install -c $Constraints -e ".[dev,build]"
    if ($LASTEXITCODE -ne 0) { throw "Não foi possível instalar as dependências" }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "As dependências instaladas são incompatíveis" }
}
finally {
    Pop-Location
    if ($null -eq $PreviousConstraint) {
        Remove-Item Env:PIP_CONSTRAINT -ErrorAction SilentlyContinue
    }
    else {
        $env:PIP_CONSTRAINT = $PreviousConstraint
    }
}

"Preparação concluída. Executa .\scripts\run.ps1"
