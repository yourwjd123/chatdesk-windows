@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows.ps1" -Mode Build
if errorlevel 1 pause
