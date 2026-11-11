# Builds dist\AIUsage\AIUsage.exe (no Python needed to run it). Usage: .\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = "$PSScriptRoot\.venv\Scripts\python.exe"
New-Item -ItemType Directory -Force build | Out-Null
& $py -c "from usage_tray import draw; draw.app_icon(256).save(r'build\app.ico', sizes=[(16,16),(20,20),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE) { throw 'icon step failed' }
Get-Process AIUsage -ErrorAction SilentlyContinue | Stop-Process -Force  # the exe is locked while running
& "$PSScriptRoot\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --log-level WARN `
    --name AIUsage --icon "$PSScriptRoot\build\app.ico" `
    --add-data "$PSScriptRoot\usage_tray\logo.png;usage_tray" `
    --add-data "$PSScriptRoot\usage_tray\logos;usage_tray\logos" `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build\work" --specpath "$PSScriptRoot\build" `
    "$PSScriptRoot\main.pyw"
if ($LASTEXITCODE) { throw 'pyinstaller failed' }
Write-Host "Built $PSScriptRoot\dist\AIUsage\AIUsage.exe"
