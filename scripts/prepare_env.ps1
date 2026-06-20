$UseCurrentPython = $args -contains "-UseCurrentPython"
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

$venv = ".venv"
$py = if ($UseCurrentPython) { "python" } else { Join-Path $venv "Scripts\python.exe" }
$check = "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
$out = "tools\reasy_tex_gdeflate_helper\bin\Release\net9.0\win-x64\publish"
$pyStamp = Join-Path $venv ".reasy-python.stamp"
$helperStamp = Join-Path $out ".reasy-helper.stamp"
$helperFiles = "reasy_tex_gdeflate_helper.exe", "reasy_tex_gdeflate_helper.dll", "reasy_tex_gdeflate_helper.runtimeconfig.json", "reasy_tex_gdeflate_helper.deps.json", "runtimes\win-x64\native\libGDeflate.dll"
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
    if (-not $UseCurrentPython -and (Fresh $pyStamp @("requirements.txt", "setup.py", "native\*.c", "scripts\compile_qm.py", "resources\i18n\*.ts") @("fast_pakresolve*.pyd", "fastmesh*.pyd", "resources\i18n\*.qm"))) { return $true }
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

if (-not (Fresh $helperStamp @("scripts\prepare_env.ps1", "tools\reasy_tex_gdeflate_helper\*.cs", "tools\reasy_tex_gdeflate_helper\*.csproj") ($helperFiles | ForEach-Object { Join-Path $out $_ }))) {
    if (-not (OK "dotnet" @("publish", "tools\reasy_tex_gdeflate_helper\reasy_tex_gdeflate_helper.csproj", "-c", "Release", "-r", "win-x64", "--self-contained", "true", "-o", $out))) { exit 1 }
    Set-Content $helperStamp (Get-Date).ToUniversalTime().ToString("o")
}
foreach ($f in $helperFiles) {
    if (-not (Test-Path (Join-Path $out $f))) { Write-Error "Missing helper artifact: $f"; exit 1 }
}
