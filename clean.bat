@echo off
chcp 65001 >nul
setlocal EnableExtensions
title 番茄小说一键下载器 - 环境清理
cd /d "%~dp0"

echo ============================================
echo   番茄小说一键下载器 - 环境清理
echo ============================================
echo.
echo 将删除以下「运行时生成」的内容（源码文件不受影响）：
echo    [1] .venv / venv          虚拟环境
echo    [2] __pycache__ / *.pyc   Python 字节码缓存
echo    [3] downloads\*.txt       已导出的 TXT 文件
echo    [4] .DS_Store / Thumbs.db 系统临时文件
echo    [5] .fanqie_device.json   匿名设备 / 正文密钥缓存
echo.
set /p "CONFIRM=确认清理请输入 Y 后回车（直接回车取消）: "
if /i not "%CONFIRM%"=="Y" (
    echo.
    echo 已取消，未做任何修改。
    echo.
    pause
    exit /b 0
)

echo.
echo 正在清理...

if not exist ".venv" if not exist "venv" echo [1/5] 未发现虚拟环境，无需清理

if exist ".venv" (
    rmdir /s /q ".venv" 2>nul
    if exist ".venv" (
        echo [警告] .venv 删除失败，可能有程序正在使用它。
        echo        请先关闭正在运行的服务窗口（run.bat）后重试。
    ) else (
        echo [1/5] 已删除 .venv
    )
)
if exist "venv" (
    rmdir /s /q "venv" 2>nul
    if exist "venv" (
        echo [警告] venv 删除失败，请关闭占用程序后重试。
    ) else (
        echo [1/5] 已删除 venv
    )
)

for /d /r "%~dp0." %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul
echo [2/5] 已清理 __pycache__

for /r "%~dp0." %%f in (*.pyc) do @if exist "%%f" del /f /q "%%f" 2>nul
echo [2/5] 已清理 *.pyc

if exist "downloads" (
    for %%f in ("downloads\*.txt") do @if exist "%%~f" del /f /q "%%~f" 2>nul
)
echo [3/5] 已清空 downloads 目录中的 TXT

for /r "%~dp0." %%f in (.DS_Store Thumbs.db) do @if exist "%%f" del /f /q "%%f" 2>nul
echo [4/5] 已清理系统临时文件

set "DEVICE_CLEANED="
if exist ".fanqie_device.json" (
    del /f /q ".fanqie_device.json" 2>nul
    set "DEVICE_CLEANED=1"
)
if exist ".fanqie_device.tmp" (
    del /f /q ".fanqie_device.tmp" 2>nul
    set "DEVICE_CLEANED=1"
)
if defined DEVICE_CLEANED (
    if exist ".fanqie_device.json" (
        echo [警告] .fanqie_device.json 删除失败，请手动删除。
    ) else (
        echo [5/5] 已删除设备密钥缓存
    )
) else (
    echo [5/5] 未发现设备密钥缓存，无需清理
)

echo.
echo ============================================
echo  [完成] 运行时环境与导出文件已清理，
echo         项目已恢复到初始状态。
echo.
echo  如需完全清理：关闭本窗口，直接删除
echo  整个文件夹即可。
echo.
echo  本工具不写注册表、不安装系统服务、
echo  不修改全局 Python，删除文件夹后
echo  系统不会留下任何残留。
echo ============================================
echo.
pause
endlocal
