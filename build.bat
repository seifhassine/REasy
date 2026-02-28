@echo off
REM ***********************************************
REM Build script for REasy Editor (64-bit only)
REM ***********************************************

REM Check Python 3.12+
python -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)" 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.12+ required!
    pause
    exit /b 1
)
echo Python 3.12+ detected - OK
echo.

echo Building 64-bit version...

REM Clean up previous build artifacts if any
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist

set "HELPER_PROJ=tools\reasy_tex_gdeflate_helper\reasy_tex_gdeflate_helper.csproj"
set "HELPER_OUT=tools\reasy_tex_gdeflate_helper\bin\Release\net9.0\win-x64\publish"

echo Building gdeflate helper...
dotnet publish "%HELPER_PROJ%" -c Release -r win-x64 --self-contained false -o "%HELPER_OUT%"
if errorlevel 1 (
    echo Failed to build gdeflate helper.
    pause
    exit /b 1
)

for %%F in (
    "reasy_tex_gdeflate_helper.exe"
    "reasy_tex_gdeflate_helper.dll"
    "reasy_tex_gdeflate_helper.runtimeconfig.json"
    "reasy_tex_gdeflate_helper.deps.json"
    "runtimes\win-x64\native\libGDeflate.dll"
) do (
    if not exist "%HELPER_OUT%\%%~F" (
        echo Missing helper artifact: %%~F
        pause
        exit /b 1
    )
)

REM Build using PyInstaller with version file
pip install -r requirements.txt

REM Pre-build native extension into site-packages so PyInstaller can collect it
python setup.py build_ext --inplace
python scripts\compile_qm.py
python -m PyInstaller --onefile --windowed --icon=resources/icons/reasy_editor_logo.ico --version-file=version.txt ^
  --hidden-import fast_pakresolve --collect-binaries fast_pakresolve ^
  --hidden-import fastmesh --collect-binaries fastmesh ^
  --hidden-import texture2ddecoder --collect-all texture2ddecoder ^
  --add-binary "%HELPER_OUT%\reasy_tex_gdeflate_helper.exe;tools" ^
  --add-binary "%HELPER_OUT%\reasy_tex_gdeflate_helper.dll;tools" ^
  --add-binary "%HELPER_OUT%\runtimes\win-x64\native\libGDeflate.dll;tools" ^
  --add-data "%HELPER_OUT%\reasy_tex_gdeflate_helper.runtimeconfig.json;tools" ^
  --add-data "%HELPER_OUT%\reasy_tex_gdeflate_helper.deps.json;tools" ^
  REasy.py
  
xcopy /E /I /Y resources dist\resources
if exist dist\resources\data\dumps rmdir /S /Q dist\resources\data\dumps
if exist dist\resources\patches rmdir /S /Q dist\resources\patches
if not exist dist\resources\i18n mkdir dist\resources\i18n
xcopy /Y /I resources\i18n\ dist\resources\i18n\
copy "resources\images\reasy_guy.png" "dist\resources\images\reasy_guy.png"
if not exist dist\resources\scripts mkdir dist\resources\scripts
copy "scripts\auto_update.ps1" "dist\resources\scripts\auto_update.ps1"
copy "resources\data\dumps\rszre4.json" "dist\rszre4.json"
copy "resources\data\dumps\rszre2.json" "dist\rszre2.json"
copy "resources\data\dumps\rszre2rt.json" "dist\rszre2rt.json"
copy "resources\data\dumps\rszdmc5.json" "dist\rszdmc5.json"
copy "resources\data\dumps\rszsf6.json" "dist\rszsf6.json"
copy "resources\data\dumps\rszkunitsugami.json" "dist\rszkunitsugami.json"
copy "resources\data\dumps\rszre9.json" "dist\rszre9.json"
copy "resources\data\dumps\rszre8.json" "dist\rszre8.json"
copy "resources\data\dumps\rszre7.json" "dist\rszre7.json"
copy "resources\data\dumps\rszre7rt.json" "dist\rszre7rt.json"
copy "resources\data\dumps\rszre3.json" "dist\rszre3.json"
copy "resources\data\dumps\rszre3rt.json" "dist\rszre3rt.json"
copy "resources\data\dumps\rszreresistance.json" "dist\rszreresistance.json"
copy "resources\data\dumps\rszmhwilds.json" "dist\rszmhwilds.json"
copy "resources\data\dumps\rszo2.json" "dist\rszo2.json"
copy "resources\data\dumps\rszdd2.json" "dist\rszdd2.json"
copy "resources\data\dumps\rszmhrise.json" "dist\rszmhrise.json"
copy "resources\data\dumps\rszpragmata.json" "dist\rszpragmata.json"
copy "resources\data\dumps\rszmhst3.json" "dist\rszmhst3.json"



if errorlevel 1 (
    echo 64-bit build FAILED.
    pause
    goto end
)

REM Rename the generated executable to include _x64
if exist dist\REasy.exe (
    move /Y dist\REasy.exe dist\REasy.exe
) else (
    echo Could not find dist\REasy.exe after 64-bit build.
    pause
    goto end
)
echo 64-bit build succeeded.

echo.
echo The executable is located in the "dist" folder as:
echo    REasy.exe  (64-bit)
pause

:end
