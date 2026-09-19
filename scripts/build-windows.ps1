param(
    [string]$Python = "py",
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildVenv = Join-Path ([System.IO.Path]::GetTempPath()) ("MailArchive-Build-" + [guid]::NewGuid())

function Assert-NativeCommandSucceeded {
    param([string]$Description)
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

Push-Location $ProjectRoot
try {
    if ($Python -eq "py") {
        & $Python -3.12 -m venv $BuildVenv
    }
    else {
        & $Python -m venv $BuildVenv
    }
    Assert-NativeCommandSucceeded "Creating the build environment"
    $BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
    & $BuildPython -m pip install --upgrade pip
    Assert-NativeCommandSucceeded "Upgrading pip"
    & $BuildPython -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")
    Assert-NativeCommandSucceeded "Installing build dependencies"
    & $BuildPython -m mailarchive.provider_config
    Assert-NativeCommandSucceeded "Validating the bundled Microsoft public client ID"
    & $BuildPython -m unittest discover -s tests -v
    Assert-NativeCommandSucceeded "Running tests"
    $SpecDir = Join-Path $ProjectRoot "build\spec"
    New-Item -ItemType Directory -Path $SpecDir -Force | Out-Null
    $PyInstallerArgs = @(
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "MailArchive",
        "--icon", (Join-Path $ProjectRoot "assets\mailarchive.ico"),
        "--specpath", $SpecDir,
        "--paths", (Join-Path $ProjectRoot "src"),
        "--collect-all", "google_auth_oauthlib",
        "--collect-all", "msal",
        "--collect-submodules", "google.auth",
        "--collect-submodules", "google.oauth2",
        "--hidden-import", "pystray._win32"
    )
    $PyInstallerArgs += (Join-Path $ProjectRoot "src\mailarchive\__main__.py")
    & $BuildPython -m PyInstaller @PyInstallerArgs
    Assert-NativeCommandSucceeded "Building the Windows application"
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "LICENSE") `
        -Destination (Join-Path $ProjectRoot "dist\MailArchive\LICENSE")
    $BuiltExecutable = Join-Path $ProjectRoot "dist\MailArchive\MailArchive.exe"
    & $BuiltExecutable --smoke-test
    Assert-NativeCommandSucceeded "Smoke testing the built Windows application"

    $Version = & $BuildPython -c "import mailarchive; print(mailarchive.__version__)"
    Assert-NativeCommandSucceeded "Reading the application version"
    if (-not $InnoCompiler) {
        $Candidates = @(
            (Join-Path ${env:ProgramFiles} "Inno Setup 7\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
        )
        $InnoCompiler = $Candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $InnoCompiler -or -not (Test-Path $InnoCompiler)) {
        throw "Inno Setup was not found. Install Inno Setup or pass -InnoCompiler C:\Path\ISCC.exe."
    }
    & $InnoCompiler `
        "/DMyAppVersion=$Version" `
        (Join-Path $ProjectRoot "packaging\windows\mailarchive.iss")
    Assert-NativeCommandSucceeded "Building the Windows installer"
}
finally {
    Pop-Location
    if (Test-Path -LiteralPath $BuildVenv) {
        Remove-Item -LiteralPath $BuildVenv -Recurse -Force
    }
}

Write-Host "Done: $ProjectRoot\dist\installer\MailArchive-Setup-$Version-x64.exe"
