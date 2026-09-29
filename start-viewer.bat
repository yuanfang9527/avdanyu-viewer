@echo off
cd /d %~dp0
title avdanyu-viewer
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python is not found in PATH!
    echo Please install Python 3 and add it to PATH.
    pause
    exit /b 1
)
python scripts\start-viewer.py
if errorlevel 1 pause
