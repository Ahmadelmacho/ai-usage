# Installs or updates AI Usage from the latest GitHub release. One command, for both:
#   irm https://raw.githubusercontent.com/Ahmadelmacho/ai-usage/main/get.ps1 | iex
# Downloads AIUsage.zip, checks its SHA-256 against the digest GitHub lists for it, unzips it and runs
# the install.ps1 inside (which stops the old app, swaps the folder in and starts the new one).
# No param() block: under irm | iex it would run in the user's own shell and reset their $Dest or $Api.
& {  # own scope, so 'Stop', the progress setting and these variables don't leak into the shell that ran irm | iex
    # For tests/test_get.py only: a fake API, and install.ps1's test-only -Dest and -CopyOnly
    $Api = if ($env:AIUSAGE_TEST_API) { $env:AIUSAGE_TEST_API } else { 'https://api.github.com' }
    $Dest = $env:AIUSAGE_TEST_DEST
    $CopyOnly = $env:AIUSAGE_TEST_COPYONLY -eq '1'
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'  # Windows PowerShell 5.1 downloads far slower with the progress bar
    $tmp = $null
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $rel = Invoke-RestMethod -UseBasicParsing -Uri "$($Api.TrimEnd('/'))/repos/Ahmadelmacho/ai-usage/releases/latest" `
            -Headers @{ 'User-Agent' = 'ai-usage-installer'; 'Accept' = 'application/vnd.github+json' }
        $asset = @($rel.assets | Where-Object { $_.name -eq 'AIUsage.zip' })[0]
        if (-not $asset) { throw "release $($rel.tag_name) has no AIUsage.zip" }
        if ("$($asset.digest)" -notmatch '^sha256:([0-9a-fA-F]{64})$') { throw "release $($rel.tag_name) lists no SHA-256 for AIUsage.zip, so it can't be checked" }
        $want = $Matches[1]
        Write-Host "Downloading AI Usage $($rel.tag_name) ..."
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ('AIUsage-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory $tmp | Out-Null
        $zip = Join-Path $tmp 'AIUsage.zip'
        $curl = Join-Path $env:SystemRoot 'System32\curl.exe'  # in Windows since 10 1803
        if (Test-Path -LiteralPath $curl) {
            # A progress bar, and a download that stalls (under 1 KB/s for 20 s, e.g. a dropped connection) starts
            # again, up to 5 times. Invoke-WebRequest showed nothing and could wait on a dead connection for minutes.
            $ErrorActionPreference = 'Continue'  # curl draws its bar on stderr, which 'Stop' would treat as an error
            # -q first: ignore any .curlrc (it could turn off the certificate check); redirects stay on https
            & $curl -q -fL --proto-redir '=https' --progress-bar --retry 5 --retry-delay 2 --connect-timeout 20 --speed-limit 1000 --speed-time 20 `
                -A 'ai-usage-installer' -o $zip $asset.browser_download_url
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE) { throw "the download failed (curl exit $LASTEXITCODE). Check the connection and try again." }
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $zip -Headers @{ 'User-Agent' = 'ai-usage-installer' }
        }
        # .NET, not Get-FileHash/Expand-Archive: those are script modules, and Windows PowerShell started under a
        # pwsh 7 PSModulePath (e.g. by an app launched from a pwsh 7 terminal) loads pwsh 7's copies, which fail
        $stream = [IO.File]::OpenRead($zip)
        try { $got = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) }
        finally { $stream.Dispose() }
        if ($got -ne $want) { throw "the download is damaged or changed (SHA-256 $got, expected $want). Nothing was installed." }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, "$tmp\app")
        $install = @(Get-ChildItem -LiteralPath "$tmp\app" -Recurse -Filter 'install.ps1')[0]
        if (-not $install) { throw 'the zip has no install.ps1' }
        # A child PowerShell with Bypass: the default policy (Restricted) would refuse to run the .ps1 file in this one
        $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $install.FullName)
        if ($Dest) { $argv += @('-Dest', $Dest) }
        if ($CopyOnly) { $argv += '-CopyOnly' }
        $ps = Join-Path $PSHOME $(if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })  # not ISE's host
        & $ps @argv  # prints its own "Install failed: ..." and keeps the old app on failure
    } catch {
        Write-Host "`nInstall failed: $($_.Exception.Message)" -ForegroundColor Red
    } finally {
        if ($tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
