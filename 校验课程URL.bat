@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo   [错误] 未找到 .venv，请先建立虚拟环境。
    pause
    exit /b 1
)

if "%~1"=="" (
    ".venv\Scripts\python.exe" "tools\check_course_url.py"
    echo.
    pause
) else (
    ".venv\Scripts\python.exe" "tools\check_course_url.py" %*
)
