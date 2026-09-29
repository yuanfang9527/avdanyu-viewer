@echo off
cd /d %~dp0
title stop-viewer
where python >nul 2>nul
if %errorlevel% neq 0 (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8971 " ^| findstr "LISTENING"') do (
        taskkill /PID %%a /F >nul 2>&1
    )
    exit /b
)
python scripts\stop-viewer.py
if errorlevel 1 pause
