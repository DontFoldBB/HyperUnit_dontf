@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM --- выбрать Python: локальный .venv -> общий venv из first_try -> системный ---
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=..\first_try\hyperliquid_trade\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" app\main.py %*
echo.
pause
