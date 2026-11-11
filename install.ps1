# Adds AI Usage to the Start menu and to Windows startup, starts it, and pins both tray numbers
# next to the clock (once; if you later hide one in Settings, this never overrides that).
$ErrorActionPreference = 'Stop'
$exe = "$PSScriptRoot\dist\AIUsage\AIUsage.exe"
if (-not (Test-Path $exe)) { throw "Build first: .\build.ps1" }

$shell = New-Object -ComObject WScript.Shell
foreach ($spot in @(
        @{ Dir = [Environment]::GetFolderPath('Programs'); Args = '' },
        @{ Dir = [Environment]::GetFolderPath('Startup'); Args = '--hidden' })) {
    $lnk = $shell.CreateShortcut((Join-Path $spot.Dir 'AI Usage.lnk'))
    $lnk.TargetPath = $exe
    $lnk.Arguments = $spot.Args
    $lnk.WorkingDirectory = Split-Path $exe
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = 'Claude Code and Codex usage limits'
    $lnk.Save()
}

Get-Process AIUsage -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process $exe

# Windows lists every tray icon under HKCU\Control Panel\NotifyIconSettings; IsPromoted=1 shows it on the taskbar.
$promoted = 0
for ($i = 0; $i -lt 20 -and $promoted -lt 2; $i++) {
    Start-Sleep -Milliseconds 500
    $promoted = 0
    foreach ($k in Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings' -ErrorAction SilentlyContinue) {  # no such key on Windows 10
        $p = Get-ItemProperty $k.PSPath
        if ($p.ExecutablePath -eq $exe) {
            if ($null -eq $p.IsPromoted) { Set-ItemProperty $k.PSPath -Name IsPromoted -Value 1 -Type DWord }
            $promoted++
        }
    }
}
Write-Host "Installed. Tray icons found: $promoted of 2."
