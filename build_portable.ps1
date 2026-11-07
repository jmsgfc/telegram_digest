$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "Python virtual environment is missing."
}

Push-Location $ProjectDir
try {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --name TelegramDigest `
        --paths src `
        --collect-submodules telethon `
        --hidden-import pyaes `
        --hidden-import rsa `
        --hidden-import rsa.core `
        --hidden-import pyasn1 `
        package_gui.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
