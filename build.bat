@echo off
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PYTHONNOUSERSITE=1"

if exist build rmdir /S /Q build || exit /b 1
if exist dist rmdir /S /Q dist || exit /b 1

if /I "%GITHUB_ACTIONS%"=="true" (
  set "PY=python"
  call "%~dp0prepare_env.bat" -UseCurrentPython || exit /b 1
) else (
  set "PATH=%CD%\.venv\Scripts;%PATH%"
  call "%~dp0prepare_env.bat" || exit /b 1
)
"%PY%" scripts\fetch_dxc.py || exit /b 1

rem PyInstaller bundles DLLs it finds on PATH, so build on a fixed one.
for /f "delims=" %%i in ('%PY% -c "import sys; print(sys.executable)"') do set "PY=%%i"
set "PATH=%SystemRoot%\System32;%SystemRoot%"

"%PY%" -c "import runpy, sys, sysconfig; sys.path.insert(0, sysconfig.get_path('stdlib')); runpy.run_module('PyInstaller', run_name='__main__')" REasy.spec || exit /b 1

xcopy /E /I /Y resources dist\resources || exit /b 1
if exist dist\resources\data\dumps rmdir /S /Q dist\resources\data\dumps
if not exist dist\resources\scripts mkdir dist\resources\scripts
copy "scripts\auto_update.ps1" "dist\resources\scripts\auto_update.ps1" || exit /b 1
copy "resources\data\dumps\*.json" dist\ || exit /b 1
copy LICENSE.txt dist\ || exit /b 1
copy TRADEMARKS.md dist\ || exit /b 1
"%PY%" scripts\collect_licenses.py dist\REasy.exe dist\resources\licenses || exit /b 1

if not exist dist\REasy.exe exit /b 1
echo Built dist\REasy.exe
