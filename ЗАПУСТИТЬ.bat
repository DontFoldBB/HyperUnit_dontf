@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM --- pick Python: local .venv -> system python ---
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app\main.py %*
echo.
pause
