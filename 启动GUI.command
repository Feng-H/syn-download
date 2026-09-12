#!/bin/bash
# 双击启动群晖下载器 GUI(macOS)
cd "$(dirname "$0")"

PY=""
if [ -x ".venv/bin/python" ] && ".venv/bin/python" -c "import tkinter, requests" 2>/dev/null; then
    PY=".venv/bin/python"
elif /usr/bin/python3 -c "import tkinter, requests" 2>/dev/null; then
    PY="/usr/bin/python3"
else
    echo "首次运行:正在初始化运行环境(需要网络,约 1 分钟)…"
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv && .venv/bin/pip install -q requests && PY=".venv/bin/python"
    fi
fi

if [ -n "$PY" ]; then
    exec "$PY" syn_dl_gui.py
else
    echo "✗ 未找到可用的 Python(需同时带 tkinter 和 requests)。"
    echo "  可执行:brew install python-tk && python3 -m venv .venv && .venv/bin/pip install requests"
    read -r -p "按回车键关闭…" _
fi
