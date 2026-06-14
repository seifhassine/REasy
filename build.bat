@echo off
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "HELPER_OUT=tools\reasy_tex_gdeflate_helper\bin\Release\net9.0\win-x64\publish"
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

"%PY%" -m PyInstaller --onefile --windowed --icon=resources/icons/reasy_editor_logo.ico --version-file=version.txt ^
  --hidden-import fast_pakresolve --collect-binaries fast_pakresolve ^
  --hidden-import fastmesh --collect-binaries fastmesh ^
  --hidden-import texture2ddecoder --collect-all texture2ddecoder ^
  --add-data "%HELPER_OUT%;tools" ^
  REasy.py || exit /b 1

xcopy /E /I /Y resources dist\resources || exit /b 1
if exist dist\resources\data\dumps rmdir /S /Q dist\resources\data\dumps
if exist dist\resources\patches rmdir /S /Q dist\resources\patches
if not exist dist\resources\i18n mkdir dist\resources\i18n
xcopy /Y /I resources\i18n\ dist\resources\i18n\ || exit /b 1
copy "resources\images\reasy_guy.png" "dist\resources\images\reasy_guy.png" || exit /b 1
if not exist dist\resources\scripts mkdir dist\resources\scripts
copy "scripts\auto_update.ps1" "dist\resources\scripts\auto_update.ps1" || exit /b 1
copy "resources\data\dumps\*.json" dist\ || exit /b 1

if not exist dist\REasy.exe exit /b 1
echo Built dist\REasy.exe
