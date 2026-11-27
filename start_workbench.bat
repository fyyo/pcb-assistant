@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [错误] 未找到虚拟环境 Python: "%PY%"
    echo 请先按 docs\DEVELOPMENT.md 安装 .venv 依赖。
    pause
    exit /b 1
)

set "CATALOG=%~1"
if "%CATALOG%"=="" set "CATALOG=data\phase3_catalog_v9"

echo 正在启动 PCB 压合排查工作台 (http://127.0.0.1:8765/) ...
"%PY%" -X utf8 -B knowledge_catalog.py --catalog "%CATALOG%" web --open
if errorlevel 1 pause
endlocal
