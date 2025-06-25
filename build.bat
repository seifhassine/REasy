@echo off
REM ***********************************************
REM Build script for REasy Editor (64-bit only)
REM ***********************************************

echo Building 64-bit version...

REM Clean up previous build artifacts if any
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist
if exist REasy.spec del /Q REasy.spec

REM Build using the current Python interpreter (assumed 64-bit)
pip install -r requirements.txt
python -m PyInstaller --onefile --windowed --icon=resources/icons/reasy_editor_logo.ico REasy.py
xcopy /E /I /Y resources dist\resources
rmdir /S /Q dist\resources\data\dumps
copy "resources\data\dumps\rszre4_reasy.json" "dist\rszre4_reasy.json"
copy "resources\data\dumps\rszre2.json" "dist\rszre2.json"
copy "resources\data\dumps\rszre2rt.json" "dist\rszre2rt.json"
copy "resources\data\dumps\rszdmc5.json" "dist\rszdmc5.json"
copy "resources\data\dumps\rszsf6.json" "dist\rszsf6.json"
copy "resources\data\dumps\rszre8.json" "dist\rszre8.json"
copy "resources\data\dumps\rszre7.json" "dist\rszre7.json"
copy "resources\data\dumps\rszre7rt.json" "dist\rszre7rt.json"
copy "resources\data\dumps\rszre3.json" "dist\rszre3.json"
copy "resources\data\dumps\rszre3rt.json" "dist\rszre3.json"
copy "resources\data\dumps\rszreresistance.json" "dist\rszreresistance.json"
copy "resources\data\dumps\rszmhwilds.json" "dist\rszmhwilds.json"
copy "resources\data\dumps\rszo2.json" "dist\rszo2.json"
copy "resources\data\dumps\rszdd2.json" "dist\rszdd2.json"



if errorlevel 1 (
    echo 64-bit build FAILED.
    pause
    goto end
)

REM Rename the generated executable to include _x64
if exist dist\REasy.exe (
    move /Y dist\REasy.exe dist\REasy_x64.exe
) else (
    echo Could not find dist\REasy.exe after 64-bit build.
    pause
    goto end
)
echo 64-bit build succeeded.

echo.
echo The executable is located in the "dist" folder as:
echo    REasy_x64.exe  (64-bit)
pause

:end
