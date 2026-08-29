[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$EntryPoint = Join-Path $Root "src\organizador\main.py"
$Executable = Join-Path $Root "dist\Organizador\Organizador.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Ambiente não encontrado. Executa primeiro .\scripts\setup.ps1"
}

& $Python -m ruff check (Join-Path $Root "src") (Join-Path $Root "tests") (Join-Path $Root "scripts")
if ($LASTEXITCODE -ne 0) { throw "ruff falhou" }

& $Python -m mypy (Join-Path $Root "src\organizador")
if ($LASTEXITCODE -ne 0) { throw "mypy falhou" }

$env:QT_QPA_PLATFORM = "offscreen"
& $Python -m pytest (Join-Path $Root "tests")
if ($LASTEXITCODE -ne 0) { throw "pytest falhou" }
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "Organizador" `
    --paths (Join-Path $Root "src") `
    --hidden-import "watchdog.observers.winapi" `
    $EntryPoint
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou" }

if (-not (Test-Path -LiteralPath $Executable)) {
    throw "O executável esperado não foi criado: $Executable"
}

$SmokeRoot = Join-Path $env:TEMP ("organizador-smoke-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $SmokeRoot | Out-Null
    $SmokeArguments = "--smoke-test --data-dir `"$SmokeRoot`""
    $SmokeProcess = Start-Process `
        -FilePath $Executable `
        -ArgumentList $SmokeArguments `
        -Wait `
        -PassThru
    if ($SmokeProcess.ExitCode -ne 0) {
        throw "O executável não passou o arranque de teste"
    }
}
finally {
    if (Test-Path -LiteralPath $SmokeRoot) {
        Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
    }
}

"Build concluído: $Executable"
