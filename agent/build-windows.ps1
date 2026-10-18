param([string]$Python = "python")
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Build this executable on Windows." }
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $repoRoot "build"
$distRoot = Join-Path $repoRoot "dist"
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onefile --console --noupx `
    --name StintaraAgent --distpath $distRoot --workpath (Join-Path $buildRoot "agent") `
    --specpath $buildRoot (Join-Path $PSScriptRoot "stintara_agent.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
# Only the public example is distributed; never copy the operator config.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "stintara-agent.example.json") -Destination $distRoot
Write-Output "Built dist/StintaraAgent.exe and the public example configuration."
