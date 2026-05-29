@echo off
chcp 65001 >nul
echo ========================================
echo   dsv4-cc-proxy-tray 打包工具
echo ========================================
echo.

REM 检查 pyinstaller
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    echo [1/4] 安装 pyinstaller...
    pip install pyinstaller
)

echo [2/4] 安装依赖（确保 httpx 等被打包）...
pip install httpx starlette uvicorn anyio httpcore h11 certifi idna

echo [3/4] 打包中...
pyinstaller --onefile --windowed --name dsv4-cc-proxy-tray --icon Logo.ico --clean --add-data "dsv4_cc_proxy;dsv4_cc_proxy" --hidden-import tkinter --hidden-import httpx --hidden-import starlette --hidden-import uvicorn --hidden-import anyio --hidden-import httpcore --hidden-import h11 --hidden-import certifi --collect-all httpx --collect-all httpcore dsv4_cc_proxy/gui.py

echo.
echo [4/4] 完成！
echo 输出文件: dist\dsv4-cc-proxy-tray.exe
echo.

REM 清理
rmdir /s /q build 2>nul
del /q dsv4-cc-proxy-tray.spec 2>nul

echo 体积:
dir dist\dsv4-cc-proxy-tray.exe 2>nul | find "dsv4-cc-proxy-tray.exe"
pause
