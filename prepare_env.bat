@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prepare_env.ps1" %*
