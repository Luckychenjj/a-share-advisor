@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src"

where python >nul 2>nul
if %errorlevel%==0 (
  python -m xiao_a.cli dashboard
) else (
  py -3 -m xiao_a.cli dashboard
)

if errorlevel 1 (
  echo.
  echo 启动失败：请先安装 Python 3.11 或更高版本。
  pause
)
