@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo   [错误] 未找到 .venv 虚拟环境（图形界面需要它）。
    echo.
    echo   请在项目目录下执行下面两行来创建：
    echo       python -m venv .venv
    echo       .venv\Scripts\pip install playwright
    echo.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "app\run.py" --action gui
exit /b 0
