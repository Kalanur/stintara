# Starts the normal private Pitwall wire recorder (python -m tools.replay.capture). No capture logic lives here.
# Run from the activated development environment, or pass -Python. Nothing is installed; Ctrl+C stops the recording.
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$Track = "",
    [string]$Event = "",
    [string]$DriverAlias = "",
    [string]$OutputDir = "",
    [string]$PitwallWs = ""
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $repoRoot ".venv/Scripts/python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
}
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python not found. Activate the development environment or pass -Python <path>."
}
# Each value is a separate array element, so paths and labels with spaces stay intact.
$arguments = @("-m", "tools.replay.capture")
foreach ($option in @(
        @("--track", $Track), @("--event", $Event), @("--driver-alias", $DriverAlias),
        @("--output-dir", $OutputDir), @("--pitwall-ws", $PitwallWs))) {
    if ($option[1]) { $arguments += $option }
}
Push-Location -LiteralPath $repoRoot
try {
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
