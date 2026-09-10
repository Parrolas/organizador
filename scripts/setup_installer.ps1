$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "artifacts\tools"
New-Item -ItemType Directory -Path $Tools -Force | Out-Null
$Installer = Join-Path $Tools "innosetup-6.7.3.exe"
Invoke-WebRequest -Uri "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe" -OutFile $Installer
$Signature = Get-AuthenticodeSignature -LiteralPath $Installer
if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notlike '*Pyrsys B.V.*') {
    throw "Inno Setup publisher verification failed"
}
$Destination = Join-Path $Tools "InnoSetup"
$Process = Start-Process -FilePath $Installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/NOICONS', "/DIR=`"$Destination`"") -WindowStyle Hidden -Wait -PassThru
if ($Process.ExitCode -ne 0) { throw "Inno Setup installation failed" }
Write-Output "Compiler: $Destination\ISCC.exe"
