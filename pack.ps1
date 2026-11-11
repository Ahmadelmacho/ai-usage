# Makes dist\AIUsage.zip to share or release: the built app plus share\Install.cmd, share\install.ps1 and LICENSE. Run .\build.ps1 first.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path 'dist\AIUsage\AIUsage.exe')) { throw 'Build first: .\build.ps1' }
$stage = 'build\pack\AI Usage'
if (Test-Path 'build\pack') { Remove-Item 'build\pack' -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item 'dist\AIUsage\*' $stage -Recurse
Copy-Item 'share\Install.cmd', 'share\install.ps1', 'LICENSE' $stage
Compress-Archive -Path 'build\pack\AI Usage' -DestinationPath 'dist\AIUsage.zip' -Force
Write-Host "Made $PSScriptRoot\dist\AIUsage.zip"
