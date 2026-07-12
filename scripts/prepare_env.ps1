$UseCurrentPython = $args -contains "-UseCurrentPython"
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

$venv = ".venv"
$py = if ($UseCurrentPython) { "python" } else { Join-Path $venv "Scripts\python.exe" }
$check = "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
$pyStamp = Join-Path $venv ".reasy-python.stamp"
$gdeflateRoot = Join-Path ".cache" "gdeflate"
$gdeflateSource = Join-Path $gdeflateRoot "GDeflateCore"
$gdeflateBuild = Join-Path $gdeflateRoot "build"
$gdeflateDll = Join-Path $gdeflateRoot "libGDeflate.dll"
$gdeflateStamp = Join-Path $gdeflateRoot ".reasy-gdeflate.stamp"
$gdeflateCoreUrl = "https://github.com/yretenai/GDeflateCore.git"
$gdeflateCoreCommit = "92e065dd0d616a3330cd37d197fc55d3b99a1a2f"
$env:PYTHONNOUSERSITE = "1"
if (-not $UseCurrentPython) { $env:PATH = "$(Join-Path (Get-Location) "$venv\Scripts");$env:PATH" }

function OK($exe, [string[]] $argv) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { return $false }
    & $exe @argv | Out-Host
    $LASTEXITCODE -eq 0
}
function Clean { if (Test-Path $venv) { Remove-Item -LiteralPath $venv -Recurse -Force } }
function Uses-SystemSite {
    $cfg = Join-Path $venv "pyvenv.cfg"
    (Test-Path $cfg) -and ((Get-Content $cfg -Raw) -match "include-system-site-packages\s*=\s*true")
}
function Fresh($stamp, $inputs, $outputs) {
    if (-not (Test-Path $stamp)) { return $false }
    foreach ($o in $outputs) { if (-not (Get-ChildItem $o -ErrorAction SilentlyContinue | Select-Object -First 1)) { return $false } }
    $t = (Get-Item $stamp).LastWriteTimeUtc
    -not (Get-ChildItem $inputs -File -ErrorAction SilentlyContinue | Where-Object LastWriteTimeUtc -gt $t | Select-Object -First 1)
}
function Prep {
    if (-not $UseCurrentPython -and (Fresh $pyStamp @("requirements.txt", "setup.py", "native\*.c", "scripts\compile_qm.py", "resources\i18n\*.ts") @("fast_pakresolve*.pyd", "fast_string_scan*.pyd", "fastmesh*.pyd", "resources\i18n\*.qm"))) { return $true }
    if (-not $UseCurrentPython -and -not (OK $py @("-m", "ensurepip", "--upgrade"))) { return $false }
    if (-not $UseCurrentPython -and -not (OK $py @("-m", "pip", "--disable-pip-version-check", "install", "-r", "requirements.txt"))) { return $false }
    if (-not (OK $py @("setup.py", "build_ext", "--inplace"))) { return $false }
    if (-not (OK $py @("scripts\compile_qm.py"))) { return $false }
    if (-not $UseCurrentPython) { Set-Content $pyStamp (Get-Date).ToUniversalTime().ToString("o") }
    return $true
}
function Try-Python($exe, [string[]] $prefix, $name) {
    Write-Output "Trying $name..."
    if (-not (OK $exe ($prefix + @("-c", $check)))) { return $false }
    Clean
    if (-not (OK $exe ($prefix + @("-m", "venv", "--system-site-packages", $venv)))) { return $false }
    if (Prep) { return $true }
    Clean
    return $false
}
function Find-VsDevCmd {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $install = (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
        if ($install) {
            $candidate = Join-Path $install "Common7\Tools\VsDevCmd.bat"
            if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
        }
    }
    $roots = @(${env:ProgramFiles}, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $roots) {
        $candidate = Get-ChildItem (Join-Path $root "Microsoft Visual Studio\*\*\Common7\Tools\VsDevCmd.bat") -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    return $null
}
function Git-InRepo($repo, [string[]] $argv) {
    $repoPath = [IO.Path]::GetFullPath($repo).Replace("\", "/")
    OK "git" (@("-c", "safe.directory=$repoPath", "-C", $repo) + $argv)
}
function Restore-GDeflateNet {
    $path = "GDeflateNet"
    $trackedDll = "GDeflateNet/runtimes/win-x64/native/libGDeflate.dll"
    if (-not (Test-Path (Join-Path $path ".git"))) {
        $url = (& git config -f .gitmodules --get submodule.GDeflateNet.url).Trim()
        $commit = (& git rev-parse HEAD:GDeflateNet).Trim()
        if (-not $url -or -not $commit) { Write-Error "Could not resolve the GDeflateNet submodule configuration."; return $false }
        if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        if (-not (OK "git" @("clone", "--no-checkout", $url, $path))) { return $false }
        if (-not (Git-InRepo $path @("fetch", "--depth", "1", "origin", $commit))) { return $false }
        if (-not (Git-InRepo $path @("checkout", "--detach", "--force", $commit))) { return $false }
    }
    if (-not (Test-Path (Join-Path $path $trackedDll))) {
        if (-not (Git-InRepo $path @("restore", "--source=HEAD", "--", $trackedDll))) { return $false }
    }
    return $true
}
function Build-GDeflate {
    if (Fresh $gdeflateStamp @("requirements.txt", "scripts\prepare_env.ps1") @($gdeflateDll)) { return $true }

    New-Item -ItemType Directory -Force $gdeflateRoot | Out-Null
    if (-not (Test-Path (Join-Path $gdeflateSource ".git"))) {
        if (Test-Path $gdeflateSource) { Remove-Item -LiteralPath $gdeflateSource -Recurse -Force }
        if (-not (OK "git" @("clone", $gdeflateCoreUrl, $gdeflateSource))) { return $false }
    }
    if (-not (Git-InRepo $gdeflateSource @("fetch", "--depth", "1", "origin", $gdeflateCoreCommit))) { return $false }
    if (-not (Git-InRepo $gdeflateSource @("checkout", "--detach", "--force", $gdeflateCoreCommit))) { return $false }

    $vsDevCmd = Find-VsDevCmd
    if (-not $vsDevCmd) { Write-Error "Visual Studio C++ Build Tools are required to build libGDeflate.dll."; return $false }
    $pythonExe = (& $py -c "import sys; print(sys.executable)").Trim()
    if (-not $pythonExe) { return $false }

    $sourcePath = (Resolve-Path $gdeflateSource).Path
    $buildPath = [IO.Path]::GetFullPath($gdeflateBuild)
    $installPath = Join-Path ([IO.Path]::GetFullPath($gdeflateRoot)) "install"
    $meson = '"' + $pythonExe + '" -m mesonbuild.mesonmain'
    $setupMode = if (Test-Path (Join-Path $gdeflateBuild "meson-private\coredata.dat")) { "--reconfigure" } else { "" }
    $setup = 'call "' + $vsDevCmd + '" -arch=amd64 && ' + $meson + ' setup "' + $buildPath + '" "' + $sourcePath + '" ' + $setupMode + ' --prefix "' + $installPath + '" --buildtype release -Ddefault_library=shared'
    & cmd.exe /d /c $setup
    if ($LASTEXITCODE -ne 0) { return $false }

    $compile = 'call "' + $vsDevCmd + '" -arch=amd64 && ' + $meson + ' compile -C "' + $buildPath + '"'
    & cmd.exe /d /c $compile
    if ($LASTEXITCODE -ne 0) { return $false }

    $builtDll = Get-ChildItem (Join-Path $gdeflateBuild "subprojects\GDeflate") -Filter "GDeflate-*.dll" -File | Select-Object -First 1
    if (-not $builtDll) { Write-Error "GDeflate build completed without producing a DLL."; return $false }
    Copy-Item -LiteralPath $builtDll.FullName -Destination $gdeflateDll -Force
    Set-Content $gdeflateStamp $gdeflateCoreCommit
    return $true
}

$ready = ($UseCurrentPython -or ((Test-Path $py) -and (Uses-SystemSite))) -and (OK $py @("-c", $check)) -and (Prep)
if (-not $ready) {
    if ($UseCurrentPython) { Write-Error "Python 3.12+ with dependencies is required."; exit 1 }
    Clean
    $versions = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $versions = & py -0p 2>$null | ForEach-Object { if ($_ -match "^\s*(-V:3(?:\.\d+)?)\b") { $Matches[1] } }
    }
    foreach ($v in $versions) { if (Try-Python "py" ([string[]]@($v)) $v) { $ready = $true; break } }
    if (-not $ready) { $ready = Try-Python "python" ([string[]]@()) "python" }
}
if (-not $ready) { Write-Error "Python 3.12+ with pip is required."; exit 1 }

if (-not (Restore-GDeflateNet)) { Write-Error "Failed to initialize or repair the GDeflateNet submodule."; exit 1 }
if (-not (Build-GDeflate)) { Write-Error "Failed to build libGDeflate.dll from source."; exit 1 }
