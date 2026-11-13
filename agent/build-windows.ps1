param(
    [string]$Python = "python",
    [ValidateSet("dev", "ci", "release")][string]$Channel = "dev",
    [string]$Revision = "unknown"
)
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Build this executable on Windows." }
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $repoRoot "build"
$distRoot = Join-Path $repoRoot "dist"
$tool = Join-Path $repoRoot "tools/agent_release.py"
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
# Inject build identity (fails for non-dev builds without a full 40-char revision).
& $Python $tool build-info --channel $Channel --revision $Revision
if ($LASTEXITCODE -ne 0) { throw "Invalid build identity." }
$versionFile = Join-Path $buildRoot "agent-version-info.txt"
try {
    & $Python $tool version-file --out $versionFile
    if ($LASTEXITCODE -ne 0) { throw "Version resource generation failed." }
    & $Python -m PyInstaller --noconfirm --clean --onefile --console --noupx `
        --name StintaraAgent --distpath $distRoot --workpath (Join-Path $buildRoot "agent") `
        --specpath $buildRoot --version-file $versionFile (Join-Path $PSScriptRoot "stintara_agent.py")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
} finally {
    # Do not leave injected metadata in the working tree.
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot "_build_info.py") -ErrorAction SilentlyContinue
}
# Only the public example is distributed; never copy the operator config.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "stintara-agent.example.json") -Destination $distRoot
Write-Output "Built dist/StintaraAgent.exe ($Channel build)."
