# One-step install or update for Windows (Docker Desktop with WSL 2), in PowerShell:
#   gh api repos/jonclegg/stealthbox/contents/install.ps1 -H 'Accept: application/vnd.github.raw' | iex
#
# The repository is private, so the GitHub CLI must be signed in (gh auth login).
#
# Works in Windows PowerShell 5.1 and PowerShell 7. The app lives in ~\.stealthbox\app and is
# replaced wholesale from a GitHub zip on every run; the previous copy is kept in
# ~\.stealthbox\app.previous and your .env settings carry over.
# Optional: $env:STEALTHBOX_ZIP_URL installs from a zip at that URL instead.
#
# For agents running this for a user: without Docker Desktop the installer changes
# nothing and stops after a line starting "STEALTHBOX_NEEDS: docker". Ask the user
# whether to install it, and if they agree, set $env:STEALTHBOX_INSTALL_DOCKER = "yes"
# and run it again. "STEALTHBOX_NEEDS: user" means a step only the user can do
# (finishing Docker Desktop's first-run setup, a restart); it says which.

$ErrorActionPreference = "Stop"

function Fail($message) {
    Write-Host ""
    Write-Host "Problem: $message" -ForegroundColor Red
    Write-Host ""
    throw "Stealthbox install stopped."
}

function Needs($kind, $message) {
    Write-Host ""
    Write-Host "STEALTHBOX_NEEDS: $kind"
    Write-Host $message
    Write-Host ""
    throw "Stealthbox install stopped: needs $kind."
}

function Wait-DockerEngine($seconds) {
    for ($i = 0; $i -lt $seconds; $i += 5) {
        docker info --format "{{.OSType}}" *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Install-DockerDesktop {
    if ($env:STEALTHBOX_INSTALL_DOCKER -ne "yes") {
        Needs "docker" ("Stealthbox runs its browser in a Linux container, and Docker Desktop is not installed.`n" +
            "It would install Docker Desktop with winget (Windows asks for administrator approval):`n" +
            "  winget install -e --id Docker.DockerDesktop`n`n" +
            "To install it, run the installer again with:`n" +
            "  `$env:STEALTHBOX_INSTALL_DOCKER = `"yes`"; gh api repos/jonclegg/stealthbox/contents/install.ps1 -H 'Accept: application/vnd.github.raw' | iex")
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Needs "user" "winget is not available here. Install Docker Desktop from https://docs.docker.com/desktop/setup/install/windows-install/, open it once, then run the installer again."
    }
    Write-Host "Installing Docker Desktop with winget (approve the administrator prompt)..."
    winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Needs "user" "winget could not install Docker Desktop (exit $LASTEXITCODE). Install it from https://docs.docker.com/desktop/setup/install/windows-install/, open it once, then run the installer again."
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Start-DockerDesktop {
    $app = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $app) {
        Write-Host "Starting Docker Desktop..."
        Start-Process $app
        if (Wait-DockerEngine 240) { return }
    }
    Needs "user" ("Docker Desktop is installed but its engine is not running.`n" +
        "Open Docker Desktop from the Start menu and finish its first-run setup (accept the terms;`n" +
        "if it asks to install or update WSL, allow it and restart Windows if asked).`n" +
        "When it says 'Engine running', run the Stealthbox installer again.")
}

function Install-Stealthbox {
    $zipUrl = $env:STEALTHBOX_ZIP_URL
    if (-not $zipUrl -and -not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "Install the GitHub CLI (https://cli.github.com) and sign in with gh auth login: Stealthbox is a private repository." }
    $stateDir = Join-Path $HOME ".stealthbox"
    $installDir = Join-Path $stateDir "app"

    # Windows PowerShell 5.1 may default to TLS 1.0, which GitHub refuses.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    # Let start.ps1 run in this session even where script execution is restricted.
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Install-DockerDesktop
    }
    $osType = (docker info --format "{{.OSType}}" 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Start-DockerDesktop
        $osType = (docker info --format "{{.OSType}}" 2>$null)
    }
    if ($osType -ne "linux") {
        Fail "Docker is set to Windows containers, and Stealthbox needs Linux containers.`nRight-click the Docker icon in the taskbar, choose 'Switch to Linux containers...', then run this again."
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail "Your Docker is missing 'docker compose'. Update Docker Desktop, then run this again."
    }
    Write-Host "Container engine: Docker Desktop (Linux containers)."

    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    # The app folder can't be moved aside while this session's current folder is inside it.
    if ((Get-Location).Path -like "$installDir*") { Set-Location $HOME }
    Get-ChildItem -Path $stateDir -Filter ".download-*" -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    $stage = Join-Path $stateDir (".download-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        Write-Host "Downloading Stealthbox..."
        $zip = Join-Path $stage "app.zip"
        if ($zipUrl) { Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing }
        else { gh api repos/jonclegg/stealthbox/zipball/main > $zip; if ($LASTEXITCODE -ne 0) { Fail "Could not download Stealthbox with the GitHub CLI; sign in with gh auth login." } }
        Expand-Archive -Path $zip -DestinationPath (Join-Path $stage "x")
        $new = Get-ChildItem -Path (Join-Path $stage "x") -Directory |
            Where-Object { (Test-Path (Join-Path $_.FullName "start.ps1")) -and (Test-Path (Join-Path $_.FullName "docker-compose.yml")) } |
            Select-Object -First 1
        if (-not $new) { Fail "The download doesn't look like Stealthbox." }
        # Files from the internet are marked as such; unblock them so start.ps1 may run.
        Get-ChildItem -Path $new.FullName -Recurse -File | Unblock-File
        $oldEnv = Join-Path $installDir ".env"
        if (Test-Path $oldEnv) { Copy-Item $oldEnv (Join-Path $new.FullName ".env") }
        if (Test-Path $installDir) {
            $previous = Join-Path $stateDir "app.previous"
            if (Test-Path $previous) { Remove-Item -Recurse -Force $previous }
            Move-Item $installDir $previous
            Move-Item $new.FullName $installDir
            Write-Host "Updated Stealthbox. The previous copy is in $previous"
        } else {
            Move-Item $new.FullName $installDir
            Write-Host "Installed Stealthbox in $installDir"
        }
    } finally {
        if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    }

    Write-Host "Starting Stealthbox..."
    # start.ps1 changes the current folder; give the user theirs back afterwards.
    Push-Location
    try { & (Join-Path $installDir "start.ps1") } finally { Pop-Location }
    Write-Host ("Start again later:  & `"" + (Join-Path $installDir "start.ps1") + "`"")
    Write-Host ("Stop:               & `"" + (Join-Path $installDir "stop.ps1") + "`"")
}

Install-Stealthbox
