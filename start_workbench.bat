@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul && (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
    )
    if "%PY%"=="" (
        where python >nul 2>nul && (
            python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
        )
    )
)

if "%PY%"=="" (
    echo [错误] 未检测到 .venv 虚拟环境或系统 Python 3.11+。
    echo 请安装 Python 3.11/3.12 后重试，或参考 docs\DEVELOPMENT.md 初始化环境。
    pause
    exit /b 1
)

if /I "%~1"=="--doctor" (
    %PY% -X utf8 -B knowledge_catalog.py doctor --probe-llm
    pause
    exit /b %ERRORLEVEL%
)

if /I "%~1"=="--init-venv" (
    if not exist "%~dp0.venv\Scripts\python.exe" (
        echo 正在创建本机虚拟环境 .venv ...
        %PY% -m venv .venv || (pause & exit /b 1)
    )
    echo 正在安装核心中文分词依赖 requirements-core.txt ...
    "%~dp0.venv\Scripts\python.exe" -m pip install -r requirements\requirements-core.txt
    pause
    exit /b %ERRORLEVEL%
)

if /I "%~1"=="--shortcut" (
    %PY% -X utf8 -B knowledge_catalog.py web --create-shortcut
    pause
    exit /b %ERRORLEVEL%
)

set "CATALOG_ARG="
if not "%~1"=="" set "CATALOG_ARG=--catalog "%~1""

echo 正在启动或唤醒 PCB 压合排查工作台 ...
if "%PY%"=="py -3" (
    py -3 -X utf8 -B knowledge_catalog.py %CATALOG_ARG% web --desktop --reuse-running --auto-port
) else (
    "%PY%" -X utf8 -B knowledge_catalog.py %CATALOG_ARG% web --desktop --reuse-running --auto-port
)
if errorlevel 1 pause
endlocal
