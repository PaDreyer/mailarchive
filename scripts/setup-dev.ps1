param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DevVenv = Join-Path $ProjectRoot ".venv"

Push-Location $ProjectRoot
try {
    & $Python -3.12 -m venv $DevVenv
    $DevPython = Join-Path $DevVenv "Scripts\python.exe"
    & $DevPython -c "import tkinter"
    if ($LASTEXITCODE -ne 0) {
        throw "Tkinter is required. Repair the Python installation and include Tcl/Tk support."
    }
    & $DevPython -m pip install --upgrade pip
    & $DevPython -m pip install -e $ProjectRoot
}
finally {
    Pop-Location
}

Write-Host "Development environment is ready."
Write-Host "Activate it with: .\.venv\Scripts\Activate.ps1"
Write-Host "The editable installation runs Python modules directly from: $ProjectRoot\src"
