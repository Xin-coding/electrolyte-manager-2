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
    Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class EM2SmokeWindows {
    public delegate bool Visitor(IntPtr window, IntPtr arg);
    [DllImport("user32.dll")] static extern bool EnumWindows(Visitor callback, IntPtr arg);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint msg, IntPtr wParam, IntPtr lParam);
    public static IntPtr Find(int process, string title) {
        IntPtr result = IntPtr.Zero;
        EnumWindows((window, arg) => {
            uint pid; GetWindowThreadProcessId(window, out pid);
            if (pid == process) {
                var text = new StringBuilder(512); GetWindowText(window, text, 512);
                if (text.ToString() == title) { result = window; return false; }
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
"@
    $versionSource = Get-Content -LiteralPath (Join-Path $sourceRoot 'em2\__init__.py') -Raw
    $version = [regex]::Match($versionSource, '__version__ = "([^"]+)"').Groups[1].Value
    $title = 'Electrolyte Manager ' + $version
    $process = Start-Process -FilePath $exePath -WorkingDirectory $testRoot -WindowStyle Hidden -PassThru
    $null = $process.Handle
    $deadline = (Get-Date).AddSeconds(45)
    $window = [IntPtr]::Zero
    do {
        Start-Sleep -Milliseconds 300
        $process.Refresh()
        if ($process.HasExited) { throw 'Portable application exited before creating its window' }
        $window = [EM2SmokeWindows]::Find($process.Id, $title)
    } while ($window -eq [IntPtr]::Zero -and (Get-Date) -lt $deadline)
    if ($window -eq [IntPtr]::Zero) { throw ('Expected window not found: ' + $title) }
    $memory = $process.WorkingSet64 / 1MB
    [EM2SmokeWindows]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
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
