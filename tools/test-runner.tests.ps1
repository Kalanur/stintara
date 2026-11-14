# Lightweight runner control-flow tests; no Docker, database, Pester or network required.
$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "test.ps1"
$savedUrl = [Environment]::GetEnvironmentVariable("TEST_DATABASE_URL", "Process")
$savedLocation = Get-Location
$runnerTestState = @{}
function Assert($condition, $message) { if (-not $condition) { throw $message } }
function docker {
    $global:LASTEXITCODE = 0
    $runnerTestState.calls.Add(($args -join ' '))
    if ($runnerTestState.stderrNoise -and $args[0] -ne 'info') { Write-Error 'Container synthetic Stopping' }
    if ($args[0] -eq 'info') { return 'linux' }
    Assert ($args -contains 'stintara-tests') 'Wrong Compose project'
    Assert ($args -contains (Join-Path (Split-Path $PSScriptRoot -Parent) 'docker-compose.test.yml')) 'Wrong Compose file'
    if ($args -contains 'up') { $global:LASTEXITCODE = $runnerTestState.startupCode }
    if ($args -contains 'down') { $global:LASTEXITCODE = $runnerTestState.cleanupCode }
    if (($args -contains 'ps') -and $runnerTestState.occupied) { return 'synthetic-container-id' }
}
function TestPython {
    Assert (($args -join ' ') -eq '-m pytest -q') 'Unexpected pytest invocation'
    Assert ($env:TEST_DATABASE_URL -eq 'postgresql://stintara_test:local-test-only@127.0.0.1:55432/stintara_tests') 'Wrong test URL'
    Assert ((Get-Location).Path -eq (Split-Path $PSScriptRoot -Parent)) 'Wrong pytest directory'
    $runnerTestState.pythonCalled = $true
    $global:LASTEXITCODE = $runnerTestState.pytestCode
}
try {
    # The existing URL is a synthetic marker, never a real database or credential.
    $env:TEST_DATABASE_URL = 'synthetic-existing-value'
    foreach ($case in @(
        @{Pytest=0; Startup=0; Cleanup=0; Expected=0},
        @{Pytest=5; Startup=0; Cleanup=0; Expected=5},
        @{Pytest=0; Startup=1; Cleanup=0; Expected=$null},
        @{Pytest=0; Startup=0; Cleanup=1; Expected=1},
        @{Pytest=5; Startup=0; Cleanup=1; Expected=5}
    )) {
        $runnerTestState.calls = [Collections.Generic.List[string]]::new()
        $runnerTestState.pytestCode=$case.Pytest; $runnerTestState.startupCode=$case.Startup; $runnerTestState.cleanupCode=$case.Cleanup
        $runnerTestState.occupied=$false; $runnerTestState.pythonCalled=$false
        $failed=$false
        try { & $runner -Python TestPython -UseLocalDocker } catch { $failed=$true; Write-Output $_.Exception.Message }
        if ($null -eq $case.Expected) { Assert $failed 'Startup failure was swallowed' }
        else { Assert (-not $failed -and $LASTEXITCODE -eq $case.Expected) 'Exit code not propagated' }
        Assert ($runnerTestState.pythonCalled -eq ($case.Startup -eq 0)) 'Pytest ran before health check'
        Assert (($runnerTestState.calls | Where-Object { $_ -match 'down --volumes$' }).Count -eq 1) 'Missing teardown'
        Assert ($env:TEST_DATABASE_URL -eq 'synthetic-existing-value') 'Caller URL changed'
        Assert ((Get-Location).Path -eq $savedLocation.Path) 'Caller directory changed'
    }
    $runnerTestState.calls.Clear()
    $failed=$false
    try { & $runner -Python TestPython } catch { $failed=$true; Write-Output $_.Exception.Message }
    Assert ($failed -and $runnerTestState.calls.Count -eq 0) 'Existing URL was silently replaced'
    $runnerTestState.occupied=$true
    $failed=$false
    try { & $runner -Python TestPython -UseLocalDocker } catch { $failed=$true; Write-Output $_.Exception.Message }
    Assert $failed 'Existing test container was reused'
    Assert (-not ($runnerTestState.calls | Where-Object { $_ -match ' (up|down) ' })) 'Existing test project was changed'
    Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
    $runnerTestState.occupied=$false; $runnerTestState.pytestCode=0; $runnerTestState.cleanupCode=0
    & $runner -Python TestPython
    Assert ($LASTEXITCODE -eq 0) 'Default run failed'
    Assert ($null -eq [Environment]::GetEnvironmentVariable('TEST_DATABASE_URL', 'Process')) 'Absent URL was not restored'
    # Compose writes progress to stderr. Under Windows PowerShell 5.1 a caller-side 2>&1 turns that into
    # ErrorRecords; with $ErrorActionPreference = Stop the runner used to abort mid-teardown and lose the exit code.
    foreach ($pytestCode in 0, 5) {
        $runnerTestState.calls = [Collections.Generic.List[string]]::new()
        $runnerTestState.stderrNoise=$true; $runnerTestState.pytestCode=$pytestCode; $runnerTestState.startupCode=0; $runnerTestState.cleanupCode=0
        $runnerTestState.occupied=$false; $runnerTestState.pythonCalled=$false
        & $runner -Python TestPython
        Assert ($LASTEXITCODE -eq $pytestCode) 'stderr output changed the exit code'
        Assert (($runnerTestState.calls | Where-Object { $_ -match 'down --volumes$' }).Count -eq 1) 'stderr output prevented a single teardown'
        Assert ($null -eq [Environment]::GetEnvironmentVariable('TEST_DATABASE_URL', 'Process')) 'stderr case left the URL set'
    }
    $runnerTestState.stderrNoise=$false
    Write-Output '10 runner control-flow cases passed (mock commands; not a PostgreSQL integration test).'
} finally {
    [Environment]::SetEnvironmentVariable("TEST_DATABASE_URL", $savedUrl, "Process")
}
