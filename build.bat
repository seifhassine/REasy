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
python -m PyInstaller --onefile --windowed --icon=resources/icons/reasy_editor_logo.ico REasy.py
xcopy /E /I /Y resources dist\resources
copy "rszre4_reasy.json" "dist\rszre4_reasy.json"
copy "rszre2.json" "dist\rszre2.json"
copy "rszre2rt.json" "dist\rszre2rt.json"



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
