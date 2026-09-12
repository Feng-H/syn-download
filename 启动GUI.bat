@echo off
rem 双击启动群晖下载器 GUI(Windows)
chcp 65001 >nul
cd /d "%~dp0"

set "PYCMD="
py -3 -c "import sys" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys" >nul 2>nul && set "PYCMD=python"
if not defined PYCMD (
    echo [!] 未找到 Python,请先安装:https://www.python.org/downloads/
    echo     安装时勾选 "tcl/tk and IDLE"(默认已勾选),安装后重试本脚本。
    pause
    exit /b 1
)

%PYCMD% -c "import tkinter" >nul 2>nul || (
    echo [!] 当前 Python 缺少 tkinter,请重装 python.org 版并勾选 tcl/tk。
    pause
    exit /b 1
)
%PYCMD% -c "import requests, keyring" >nul 2>nul || (
    echo 首次运行:正在安装依赖 requests/keyring(需要网络)…
    %PYCMD% -m pip install requests keyring || %PYCMD% -m pip install requests || (echo [!] 安装失败,请检查网络后重试 & pause & exit /b 1)
)

%PYCMD% syn_dl_gui.py
if errorlevel 1 pause
