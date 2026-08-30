[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$EntryPoint = Join-Path $Root "src\organizador\main.py"
$Distribution = Join-Path $Root "dist\Organizador"
$Executable = Join-Path $Distribution "Organizador.exe"
$VersionInfo = Join-Path $Root "build\version_info.txt"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Ambiente não encontrado. Executa primeiro .\scripts\setup.ps1"
}

$Version = (& $Python -c "from organizador import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Não foi possível obter uma versão MAJOR.MINOR.PATCH válida"
}

& $Python -m ruff check (Join-Path $Root "src") (Join-Path $Root "tests") (Join-Path $Root "scripts")
if ($LASTEXITCODE -ne 0) { throw "ruff falhou" }

& $Python -m ruff format --check (Join-Path $Root "src") (Join-Path $Root "tests") (Join-Path $Root "scripts")
if ($LASTEXITCODE -ne 0) { throw "ruff format falhou" }

& $Python -m mypy (Join-Path $Root "src\organizador")
if ($LASTEXITCODE -ne 0) { throw "mypy falhou" }

$env:QT_QPA_PLATFORM = "offscreen"
& $Python -m pytest (Join-Path $Root "tests")
if ($LASTEXITCODE -ne 0) { throw "pytest falhou" }
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue

& $Python (Join-Path $Root "scripts\generate_version_info.py") $VersionInfo
if ($LASTEXITCODE -ne 0) { throw "Não foi possível gerar os metadados de versão" }

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "Organizador" `
    --paths (Join-Path $Root "src") `
    --hidden-import "watchdog.observers.winapi" `
    --version-file $VersionInfo `
    --specpath (Join-Path $Root "build") `
    --workpath (Join-Path $Root "build\pyinstaller") `
    --distpath (Join-Path $Root "dist") `
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

Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination $Distribution
Copy-Item -LiteralPath (Join-Path $Root "LICENSES") -Destination $Distribution -Recurse
& $Python (Join-Path $Root "scripts\collect_licenses.py") (Join-Path $Distribution "LICENSES")
if ($LASTEXITCODE -ne 0) { throw "Não foi possível recolher as licenças" }
$RequiredNotices = @(
    (Join-Path $Distribution "LICENSE"),
    (Join-Path $Distribution "LICENSES\GPL-3.0.txt"),
    (Join-Path $Distribution "LICENSES\LGPL-3.0.txt"),
    (Join-Path $Distribution "LICENSES\PYTHON-LICENSE.txt"),
    (Join-Path $Distribution "LICENSES\THIRD-PARTY-NOTICES.md")
)
foreach ($Notice in $RequiredNotices) {
    if (-not (Test-Path -LiteralPath $Notice)) {
        throw "Aviso de licença em falta: $Notice"
    }
}

$ReleaseDirectory = Join-Path $Root "dist\releases"
New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
$ArchiveName = "Organizador-$Version-windows-x64.zip"
$Archive = Join-Path $ReleaseDirectory $ArchiveName
$Checksum = "$Archive.sha256"
Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Checksum -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $Distribution "*") -DestinationPath $Archive -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($Checksum, "$Hash  $ArchiveName`n", $Utf8)

"Build concluído: $Executable"
"Arquivo: $Archive"
"SHA-256: $Checksum"
