@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%zuvio_gui_launcher.ps1"
if errorlevel 1 (
    echo.
    pause
)
