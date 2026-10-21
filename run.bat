@echo off
chcp 65001 >nul
setlocal EnableExtensions
title 番茄小说一键下载器
cd /d "%~dp0"

echo ============================================
echo   番茄小说一键下载器  v1.1.0
echo ============================================
echo.

REM ---------- 1. 检查 Python ----------
python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 及以上版本，
    echo        安装时务必勾选 "Add Python to PATH"。
    echo        下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM ---------- 2. 创建虚拟环境（失败时回退系统 Python） ----------
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [1/3] 首次运行，正在创建虚拟环境 .venv ...
    python -m venv "%VENV_DIR%" >nul 2>nul
)

REM 优先使用虚拟环境；venv 不可用或缺少 pip 时自动回退到系统 Python
set "PY_RUN=%VENV_PY%"
if not exist "%PY_RUN%" (
    echo [警告] 虚拟环境创建失败，将直接使用系统 Python。
    set "PY_RUN=python"
) else (
    "%PY_RUN%" -m pip --version >nul 2>nul
    if errorlevel 1 (
        echo [提示] 虚拟环境缺少 pip，正在尝试修复...
        "%PY_RUN%" -m ensurepip --upgrade >nul 2>nul
        "%PY_RUN%" -m pip --version >nul 2>nul
        if errorlevel 1 (
            echo [警告] 虚拟环境不可用，将直接使用系统 Python。
            set "PY_RUN=python"
        )
    )
)

REM ---------- 3. 安装依赖（清华镜像） ----------
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
echo [2/3] 正在检查依赖（首次安装可能需要 1-2 分钟）...
"%PY_RUN%" -m pip install --disable-pip-version-check -r requirements.txt -i %PIP_MIRROR% -q
if errorlevel 1 (
    echo [警告] 清华镜像安装失败，正在尝试官方源...
    "%PY_RUN%" -m pip install --disable-pip-version-check -r requirements.txt -q
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

REM ---------- 4. 启动服务（浏览器由 app.py 自动打开） ----------
echo [3/3] 正在启动服务...
echo 浏览器将自动打开，如未打开请手动访问: http://127.0.0.1:8000
echo 关闭此窗口或按 Ctrl+C 可停止服务。
echo.

"%PY_RUN%" app.py

echo.
echo 服务已停止。
pause
endlocal
