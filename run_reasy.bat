@echo off
cd /d "%~dp0"
set "PYTHONNOUSERSITE=1"
set "PATH=%CD%\.venv\Scripts;%PATH%"
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 15))" 2>nul || call "%~dp0prepare_env.bat" || exit /b 1
if not exist ".cache\gdeflate\libGDeflate.dll" call "%~dp0prepare_env.bat" || exit /b 1
".venv\Scripts\python.exe" REasy.py
