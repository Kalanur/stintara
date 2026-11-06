# Disposable local PostgreSQL runner. Requires Docker Compose v2 with up --wait.
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$UseLocalDocker
)
$ErrorActionPreference = "Stop"
# Handle native exit codes explicitly, including in PowerShell 7 sessions that opt in
# to turning nonzero native exit codes into PowerShell errors.
$PSNativeCommandUseErrorActionPreference = $false
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $repoRoot ".venv/Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
}
$previousUrl = [Environment]::GetEnvironmentVariable("TEST_DATABASE_URL", "Process")
if ($null -ne $previousUrl -and -not $UseLocalDocker) {
    throw "TEST_DATABASE_URL is already set. This runner will not use that database. Pass -UseLocalDocker to explicitly select the disposable local DB, or run pytest directly against your verified isolated test DB."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Install/start Docker with Linux containers and Compose v2, then retry."
}
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) { throw "Python not found. Set -Python to your development interpreter." }
$dockerOs = & docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) { throw "Docker daemon is unavailable." }
if ($dockerOs -ne "linux") { throw "The test database requires Docker Linux containers." }
$compose = @("compose", "--project-name", "stintara-tests", "--file", (Join-Path $repoRoot "docker-compose.test.yml"))
& docker @compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "Test Compose validation failed (Compose v2 required)." }
$existing = & docker @compose ps --all --quiet
if ($LASTEXITCODE -ne 0) { throw "Could not inspect test Compose project." }
if ($existing) { throw "stintara-tests already has containers. Stop the other test run or explicitly clean up that test project first." }
$result = 1
try {
    # Set cleanup responsibility before up, so partial starts are cleaned too.
    & docker @compose up --detach --wait --wait-timeout 90
    if ($LASTEXITCODE -ne 0) { throw "Test PostgreSQL startup/health check failed." }
    $env:TEST_DATABASE_URL = "postgresql://stintara_test:local-test-only@127.0.0.1:55432/stintara_tests"
    Push-Location -LiteralPath $repoRoot
    try {
        & $Python -m pytest -q
        $result = $LASTEXITCODE
    } finally { Pop-Location }
} finally {
    if ($null -eq $previousUrl) { Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue }
    else { [Environment]::SetEnvironmentVariable("TEST_DATABASE_URL", $previousUrl, "Process") }
    & docker @compose down --volumes
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Test project cleanup failed. Run docker compose --project-name stintara-tests --file docker-compose.test.yml down --volumes."
        if ($result -eq 0) { $result = 1 }
    }
}
exit $result
