#!/usr/bin/env pwsh
param(
    [switch]$Apply,
    [string]$Staged,
    [string]$Target,
    [int]$AppPid,
    [string]$ExeName = 'REasy.exe',
    [switch]$Relaunch
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host $Message }

try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$RepoOwner = 'seifhassine'
$RepoName  = 'REasy'
$ApiUrl    = "https://api.github.com/repos/$RepoOwner/$RepoName/releases/latest"

function New-Timestamp { (Get-Date).ToString('yyyyMMdd_HHmmss') }

function Test-FileUnlocked {
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $fs.Close()
        return $true
    } catch { return $false }
}

if ($Apply) {
    if (-not (Test-Path -LiteralPath $Staged)) { throw "Staged path not found: $Staged" }
    if (-not (Test-Path -LiteralPath $Target)) { throw "Target path not found: $Target" }

    if ($AppPid -gt 0) { try { Wait-Process -Id $AppPid -ErrorAction SilentlyContinue } catch {} }

    $exePath = Join-Path $Target $ExeName
    $waitUntil = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $waitUntil -and (Test-Path -LiteralPath $exePath) -and -not (Test-FileUnlocked -Path $exePath)) { Start-Sleep -Milliseconds 200 }


    function Invoke-RoboCopy {
        param([string]$Source,[string]$Destination,[switch]$Mirror)
        $qSource = '"' + $Source + '"'
        $qDest = '"' + $Destination + '"'
        $args = @($qSource, $qDest)
        # copy-only, create dirs; avoid /MIR to prevent delete-related issues
        $args += '/E'
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'robocopy'
        $psi.Arguments = [string]::Join(' ', $args)
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $proc = New-Object System.Diagnostics.Process
        $proc.StartInfo = $psi
        [void]$proc.Start()
        while (-not $proc.HasExited) {
            while (-not $proc.StandardOutput.EndOfStream) { $line = $proc.StandardOutput.ReadLine(); if ($null -ne $line) { Write-Host $line } }
            Start-Sleep -Milliseconds 50
        }
        while (-not $proc.StandardOutput.EndOfStream) { $line = $proc.StandardOutput.ReadLine(); if ($null -ne $line) { Write-Host $line } }
        $proc.WaitForExit(); return $proc.ExitCode
    }

    Write-Log "Applying update..."
    # Ensure destination root exists and pre-create expected directories from staged to avoid ERROR 3
    if (-not (Test-Path -LiteralPath $Target)) { [System.IO.Directory]::CreateDirectory($Target) | Out-Null }
    Get-ChildItem -Directory -LiteralPath $Staged -Recurse | ForEach-Object {
        $rel = $_.FullName.Substring($Staged.Length).TrimStart('\\')
        $destDir = Join-Path $Target $rel
        [void][System.IO.Directory]::CreateDirectory($destDir)
    }
    $code = Invoke-RoboCopy -Source $Staged -Destination $Target -Mirror
    if ($code -ge 8) { throw "Update failed with robocopy code $code" }

    try { Unblock-File -Path (Join-Path $Target '*') -Recurse -ErrorAction SilentlyContinue } catch {}
    try { $exe = Join-Path $Target $ExeName; if (Test-Path $exe) { Unblock-File -Path $exe -ErrorAction SilentlyContinue } } catch {}

    try {
        $stagedExe = Join-Path $Staged $ExeName
        $targetExe = Join-Path $Target $ExeName
        if ((Test-Path -LiteralPath $stagedExe) -and (Test-Path -LiteralPath $targetExe)) {
            $maxAttempts = 30
            for ($i=0; $i -lt $maxAttempts; $i++) {
                $stSize = (Get-Item $stagedExe).Length
                $tgSize = (Get-Item $targetExe).Length
                if ($stSize -eq $tgSize) { break }
                Start-Sleep -Milliseconds 200
            }
        }
    } catch {}

    try {
        if (Test-Path -LiteralPath $Staged) {
            Remove-Item -LiteralPath $Staged -Recurse -Force -ErrorAction SilentlyContinue
        }
    } catch {}

    Write-Log "Update complete."
    if ($Relaunch) {
        $exe = Join-Path $Target $ExeName
        if (Test-Path -LiteralPath $exe) {
            Start-Sleep -Milliseconds 2000
            # Place relaunch cmd inside resources\scripts and cd back to app root before starting
            $cmdPath = Join-Path $Target 'resources\scripts\relaunch_reasy.cmd'
            $lines = @(
                '@echo off',
                'cd /d "%~dp0\..\.."',
                'timeout /t 1 /nobreak >nul',
                'start "" "REasy.exe"'
            )
            [System.IO.File]::WriteAllLines($cmdPath, $lines, [System.Text.Encoding]::ASCII)
            # Launch via Explorer so cmd handles it, not PowerShell
            Start-Process -FilePath 'explorer.exe' -ArgumentList @($cmdPath)
        }
    }
    exit 0
}
else {
    $tmpParent = Join-Path $Target 'updates'
    if (-not (Test-Path -LiteralPath $tmpParent)) { [System.IO.Directory]::CreateDirectory($tmpParent) | Out-Null }
    $tmpDir = Join-Path $tmpParent ("reasy_update_" + (New-Timestamp))
    [System.IO.Directory]::CreateDirectory($tmpDir) | Out-Null
    # Do not auto-delete staged content; it will be cleaned after apply

    Write-Log "Fetching latest release metadata..."
    $headers = @{ 'Accept' = 'application/vnd.github+json' }
    $json = Invoke-RestMethod -Headers $headers -Uri $ApiUrl -TimeoutSec 15 -UseBasicParsing
    $tag    = $json.tag_name
    $assets = $json.assets
    if (-not $assets -or $assets.Count -eq 0) { throw "No assets found in latest release." }
    $asset = $assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1
    if (-not $asset) { $asset = $assets | Where-Object { ($_.content_type -like 'application/zip*' -or $_.content_type -like 'application/x-zip*') } | Select-Object -First 1 }
    if (-not $asset) { throw "No suitable .zip asset found in latest release." }
    $assetUrl = $asset.browser_download_url
    if (-not $assetUrl) { throw "Could not determine browser_download_url for release asset" }
    $tagDisplay = if ([string]::IsNullOrWhiteSpace([string]$tag)) { 'unknown' } else { [string]$tag }
    Write-Log ("Latest version: {0}, asset: {1}" -f $tagDisplay, $asset.name)

    $zipPath = Join-Path $tmpDir 'release.zip'
    Write-Log "Downloading latest release archive..."
    try {
        $req = [System.Net.HttpWebRequest]::Create($assetUrl)
        $req.UserAgent = 'REasyUpdater'
        $resp = $req.GetResponse(); $len = $resp.ContentLength
        $inStream = $resp.GetResponseStream(); $outStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
        try {
            $buffer = New-Object byte[] 65536; [long]$total = 0; [int]$lastPct = -1
            while (($read = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outStream.Write($buffer, 0, $read); $total += $read
                if ($len -gt 0) { $pct = [int]([math]::Floor(($total * 100.0) / $len)); if ($pct -ne $lastPct) { Write-Host ("PROGRESS {0}" -f $pct); $lastPct = $pct } }
            }
            if ($len -gt 0) { Write-Host "PROGRESS 100" }
        } finally { if ($outStream) { $outStream.Close() }; if ($inStream) { $inStream.Close() }; if ($resp) { $resp.Close() } }
    } catch { throw "Failed to download release archive: $($_.Exception.Message)" }

    $unpackDir = Join-Path $tmpDir 'unpacked'
    Write-Log "Extracting archive..."
    if (-not (Test-Path -LiteralPath $zipPath)) { throw "Downloaded archive not found at: $zipPath" }
    [System.IO.Directory]::CreateDirectory($unpackDir) | Out-Null
    try {
        try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue } catch {}
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $unpackDir)
    } catch {
        throw "Failed to extract archive: $($_.Exception.Message)"
    }

    $entries = Get-ChildItem -Path $unpackDir
    $dirs = @($entries | Where-Object { $_.PSIsContainer })
    $files = @($entries | Where-Object { -not $_.PSIsContainer })
    if ($dirs.Count -eq 1 -and $files.Count -eq 0) {
        $newRootPath = [System.IO.Path]::GetFullPath([string]$dirs[0])
    } else {
        $newRootPath = [System.IO.Path]::GetFullPath($unpackDir)
    }

    Write-Host "READY"
    Write-Host ("STAGED {0}" -f $newRootPath)
    Write-Host ("TARGET {0}" -f $Target)
    exit 0
}
