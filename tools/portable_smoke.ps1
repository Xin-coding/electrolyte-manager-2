$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$deliveryRoot = Split-Path -Parent $sourceRoot
$exePath = Join-Path $deliveryRoot 'app\ElectrolyteManager.exe'
$testRoot = Join-Path $deliveryRoot ('exports\portable-smoke-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$savedPath = $env:PATH
$savedEm2 = $env:EM2_HOME
$savedPython = $env:PYTHONPATH
try {
    $env:EM2_HOME = $testRoot
    $env:PYTHONPATH = ''
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    # Show the interactive deliverable for window/close acceptance; this is not
    # a background helper. A hidden window has no MainWindowTitle/close handle.
    $process = Start-Process -FilePath $exePath -WorkingDirectory $testRoot -WindowStyle Normal -PassThru
    $null = $process.Handle
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 300
        $process.Refresh()
        if ($process.HasExited) { throw 'Portable application exited before creating its window' }
    } while ($process.MainWindowTitle -ne 'Electrolyte Manager 2.0' -and (Get-Date) -lt $deadline)
    if ($process.MainWindowTitle -ne 'Electrolyte Manager 2.0') { throw ('Unexpected window: ' + $process.MainWindowTitle) }
    $title = $process.MainWindowTitle
    $memory = $process.WorkingSet64 / 1MB
    $process.CloseMainWindow() | Out-Null
    if (-not $process.WaitForExit(15000)) { throw 'Portable application did not shut down cleanly' }
    if ($process.ExitCode -ne 0) { throw 'Portable application returned a failure code' }
    $report = @{ status='passed'; executable=$exePath; title=$title; memory_MiB=$memory; no_python_path=$true; exit_code=$process.ExitCode; isolated_data=$testRoot }
    $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $deliveryRoot 'exports\portable-smoke.json') -Encoding utf8
    $report | ConvertTo-Json
} finally {
    $env:PATH = $savedPath
    $env:EM2_HOME = $savedEm2
    $env:PYTHONPATH = $savedPython
}
