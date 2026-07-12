@echo off
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PATH=%CD%\.venv\Scripts;%PATH%"
if not exist ".venv\Scripts\python.exe" call "%~dp0prepare_env.bat" || exit /b 1
if not exist ".cache\gdeflate\libGDeflate.dll" call "%~dp0prepare_env.bat" || exit /b 1
".venv\Scripts\python.exe" REasy.py
