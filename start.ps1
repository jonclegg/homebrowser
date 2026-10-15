# --force-onboard: show the first-run next steps even when the OpenRouter key is set.
$forceOnboard = $args -contains "--force-onboard"
Set-Location $PSScriptRoot

function Fail($message) {
    Write-Host ""
    Write-Host "Problem: $message" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { Fail "'docker $args' failed. See the message above." }
}

Write-Host "Stealthbox - starting the browser" -ForegroundColor White
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed.`nInstall Docker Desktop for Windows (it needs WSL 2), open it once, then run .\start.ps1 again:`n  https://www.docker.com/products/docker-desktop/"
}

$osType = (docker info --format "{{.OSType}}" 2>$null)
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Desktop is not running.`nOpen Docker Desktop from the Start menu, wait until it says `"Engine running`", then run .\start.ps1 again."
}
if ($osType -ne "linux") {
    Fail "Docker is set to Windows containers, and Stealthbox needs Linux containers.`nRight-click the Docker icon in the taskbar, choose 'Switch to Linux containers...', then run .\start.ps1 again."
}

docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Your Docker is missing 'docker compose'. Update Docker Desktop to the latest version."
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env -ErrorAction Stop
    Write-Host "Created .env with default settings (edit it later to add a proxy)."
}

# The API writes notes, run logs, photos and smoke reports here. Compose needs it
# spelled out: Windows has no HOME variable for docker-compose.yml to use.
if (-not $env:STEALTHBOX_DATA_DIR) { $env:STEALTHBOX_DATA_DIR = Join-Path $HOME ".stealthbox\data" }
New-Item -ItemType Directory -Force -Path $env:STEALTHBOX_DATA_DIR | Out-Null

Write-Host "1/3 Building Stealthbox (the first time takes several minutes)..."
Invoke-Docker compose build --quiet stealthbox

Write-Host "2/3 Starting the browser..."
Invoke-Docker compose up -d --force-recreate stealthbox

# The first start also downloads the GeoIP database (~70 MB).
Write-Host "3/3 Waiting for the browser to be ready..."
$status = "missing"
for ($i = 0; $i -lt 300; $i++) {
    $status = (docker inspect -f "{{.State.Health.Status}}" stealthbox 2>$null)
    if ($status -eq "healthy") { break }
    Start-Sleep -Seconds 2
}

if ($status -ne "healthy") {
    docker compose logs --tail 40 stealthbox
    Fail "The browser did not become ready (status: $status). See the log above, or skills/stealthbox/SKILL.md > Troubleshooting."
}

$viewerUrl = "http://" + (docker compose port stealthbox 6080)
$cdpUrl = "http://" + (docker compose port stealthbox 9222)
$apiUrl = "http://" + (docker compose port stealthbox 8423)

Write-Host ""
Write-Host "You're ready!" -ForegroundColor Green
Write-Host ""
Write-Host "  See the browser:    $viewerUrl"
Write-Host "  Chat with it:       docker exec stealthbox stealthbox chat `"<message>`"   (API $apiUrl)"
Write-Host "  Stop everything:    .\stop.ps1"
Write-Host ""
if ($forceOnboard -or -not (Test-Path (Join-Path $env:STEALTHBOX_DATA_DIR "openrouter.key"))) {
    Write-Host "  Next: docker exec stealthbox stealthbox key     (open the link it prints and paste your OpenRouter key)"
    Write-Host "        docker exec stealthbox stealthbox smoke example.com   (check it can reach sites you use;"
    Write-Host "                                                 the report is in $env:STEALTHBOX_DATA_DIR\smoke)"
    Write-Host ""
}
Write-Host "  For scripts (Playwright, Puppeteer): $cdpUrl"
Write-Host "  Only this computer can connect. Your logins are kept between restarts."
Write-Host ""
