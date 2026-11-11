# Installer shipped inside AIUsage.zip by pack.ps1. Copies the app to
# %LOCALAPPDATA%\Programs\AIUsage, adds Start menu + startup shortcuts, starts it, pins both tray numbers.
# A failed install or update puts the old copy back and starts it.
# For tests/test_install.py: -Dest is the install folder. -CopyOnly copies only (no stop, shortcuts, start or
# registry). -Menus is a folder that stands in for the Start menu and Startup folders, and skips the tray pin step.
param([string]$Dest = (Join-Path $env:LOCALAPPDATA 'Programs\AIUsage'), [switch]$CopyOnly, [string]$Menus)
$ErrorActionPreference = 'Stop'
$dest = $Dest.TrimEnd('\', '/')  # a trailing slash would put .new inside the install
$exe = "$dest\AIUsage.exe"
$new = "$dest.new"  # copy fully first, so a failed copy keeps the old install
$old = "$dest.old"  # the old install, set aside until the new one starts
$movedOld = $false
$started = $false  # past this point a failure must not touch the new copy
try {
    if (-not (Test-Path -LiteralPath "$PSScriptRoot\AIUsage.exe")) {
        throw 'Unzip first: right-click the zip > Extract All, then double-click Install.cmd in the unzipped folder.'
    }
    if (-not $CopyOnly) {
        # Only this user's copies from this install folder: stopping another session's copy is refused, and a copy
        # somewhere else is not the one being replaced.
        $session = (Get-Process -Id $PID).SessionId
        Get-Process AIUsage -ErrorAction SilentlyContinue |
            Where-Object { $_.SessionId -eq $session -and "$($_.Path)".StartsWith("$dest\", [StringComparison]::OrdinalIgnoreCase) } |
            Stop-Process -Force -PassThru | Wait-Process -Timeout 15  # Windows keeps the exe locked until it exits
    }
    $firstInstall = -not (Test-Path -LiteralPath $exe) -and -not (Test-Path -LiteralPath $old)
    if ((Resolve-Path -LiteralPath $PSScriptRoot).Path -ne $dest) {
        # A rollback that could not finish leaves the only good copy in .old: put it back, never delete it
        if ((Test-Path -LiteralPath $old) -and -not (Test-Path -LiteralPath $dest)) { Rename-Item -LiteralPath $old (Split-Path $dest -Leaf) }
        if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force }
        Copy-Item -LiteralPath $PSScriptRoot $new -Recurse
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
        if (Test-Path -LiteralPath $dest) { Rename-Item -LiteralPath $dest (Split-Path $old -Leaf); $movedOld = $true }
        Rename-Item -LiteralPath $new (Split-Path $dest -Leaf)
    }
    Get-ChildItem -LiteralPath $dest -Recurse -File | Unblock-File  # downloaded files would trip SmartScreen on every start

    if (-not $CopyOnly) {
        $programs = if ($Menus) { "$Menus\Programs" } else { [Environment]::GetFolderPath('Programs') }
        $spots = @(@{ Dir = $programs; Args = '' })
        # Startup only on a first install: an update must not bring back a startup the user turned off
        if ($firstInstall) { $spots += @{ Dir = $(if ($Menus) { "$Menus\Startup" } else { [Environment]::GetFolderPath('Startup') }); Args = '--hidden' } }
        $shell = New-Object -ComObject WScript.Shell
        foreach ($spot in $spots) {
            New-Item -ItemType Directory -Force $spot.Dir | Out-Null
            $lnk = $shell.CreateShortcut((Join-Path $spot.Dir 'AI Usage.lnk'))
            $lnk.TargetPath = $exe
            $lnk.Arguments = $spot.Args
            $lnk.WorkingDirectory = $dest
            $lnk.IconLocation = "$exe,0"
            $lnk.Description = 'Claude Code and Codex usage limits'
            $lnk.Save()
        }
        $app = Start-Process $exe -WorkingDirectory $dest -PassThru  # not the installer's folder, which the app would keep locked
        $null = $app.Handle  # keeps the exit code readable after the process ends
        # A crash at start (or antivirus ending it) exits non-zero; a second copy hands over to the first and exits 0
        if ($app.WaitForExit(3000) -and $app.ExitCode -ne 0) { throw "the new version stopped at start (exit code $($app.ExitCode))" }
        $started = $true
    }
    if ($movedOld) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }  # a leftover is harmless
    if ($CopyOnly) { Write-Host "Copied to $dest"; return }

    # IsPromoted=1 shows a tray icon on the taskbar instead of under the ^ arrow (only set once).
    # Windows 10 has no such key: there the numbers sit under the ^ arrow until dragged out.
    $promoted = 0
    for ($i = 0; $i -lt 20 -and $promoted -lt 2 -and -not $Menus -and (Test-Path 'HKCU:\Control Panel\NotifyIconSettings'); $i++) {
        Start-Sleep -Milliseconds 500
        $promoted = 0
        foreach ($k in Get-ChildItem 'HKCU:\Control Panel\NotifyIconSettings') {
            $p = Get-ItemProperty $k.PSPath
            if ($p.ExecutablePath -eq $exe) {
                if ($null -eq $p.IsPromoted) { Set-ItemProperty $k.PSPath -Name IsPromoted -Value 1 -Type DWord }
                $promoted++
            }
        }
    }

    Write-Host "`nAI Usage is installed and running. Look for two numbers next to the clock." -ForegroundColor Green
    if ($promoted -lt 2) { Write-Host "If you don't see them, click the ^ arrow by the clock and drag them out." }
    foreach ($cli in @(@{ Cmd = 'claude'; Name = 'Claude Code' }, @{ Cmd = 'codex'; Name = 'Codex (optional)' })) {
        if (-not (Get-Command $cli.Cmd -ErrorAction SilentlyContinue)) {
            Write-Host "Note: $($cli.Name) is not installed, so its number shows '-'." -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "`nInstall failed: $($_.Exception.Message)" -ForegroundColor Red
    $ErrorActionPreference = 'Continue'  # from here on, do as much of the rollback as Windows allows
    if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force -ErrorAction SilentlyContinue }
    if ($movedOld -and -not $started) {
        # Renames only, so a locked file leaves a whole copy in place, never a half-deleted one
        if (Test-Path -LiteralPath $dest) { Rename-Item -LiteralPath $dest (Split-Path $new -Leaf) -ErrorAction SilentlyContinue }
        if (-not (Test-Path -LiteralPath $dest)) { Rename-Item -LiteralPath $old (Split-Path $dest -Leaf) -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $new) { Remove-Item -LiteralPath $new -Recurse -Force -ErrorAction SilentlyContinue }
    }
    if (-not $CopyOnly -and -not $started -and (Test-Path -LiteralPath $exe)) {
        Start-Process $exe -WorkingDirectory $dest -ErrorAction SilentlyContinue
        if ($?) { Write-Host 'The app that was in place is running again.' }
    }
}
