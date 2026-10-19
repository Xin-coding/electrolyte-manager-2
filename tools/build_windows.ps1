$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$deliveryRoot = Split-Path -Parent $sourceRoot
$pythonExe = Join-Path $sourceRoot '.venv\Scripts\python.exe'
$appRoot = Join-Path $deliveryRoot 'app'
& $pythonExe -m PyInstaller --noconfirm --clean --windowed --onedir --name ElectrolyteManager --paths $sourceRoot --distpath (Join-Path $sourceRoot 'dist') --workpath (Join-Path $sourceRoot 'build') --specpath (Join-Path $sourceRoot 'build') --add-data "$sourceRoot\em2\resources;em2\resources" --exclude-module scipy --exclude-module matplotlib --exclude-module pandas --exclude-module pytest --exclude-module tkinter --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets (Join-Path $PSScriptRoot 'launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
New-Item -ItemType Directory -Force -Path $appRoot | Out-Null
Copy-Item -Path (Join-Path $sourceRoot 'dist\ElectrolyteManager\*') -Destination $appRoot -Recurse -Force
Write-Output (Join-Path $appRoot 'ElectrolyteManager.exe')
