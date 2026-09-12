#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 自动化测试:模拟 DSM,程序化驱动 App 完成登录→浏览→下载,无需人工点击。"""
import hashlib
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tkinter as tk  # noqa: E402

import syn_dl_gui  # noqa: E402
from syn_dl_gui import App  # noqa: E402
from test_e2e import DATA, Handler, MD5, REMOTE_PATH, SIZE  # noqa: E402


def pump(root, seconds, cond=None):
    """驱动 tkinter 事件循环指定时长(或直到 cond 为真)。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond is not None and cond():
            return True
        root.update()
        time.sleep(0.02)
    return cond is None or bool(cond())


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 屏蔽可能阻塞的弹窗
    dialogs = []
    for name in ("showerror", "showwarning", "showinfo", "askokcancel"):
        setattr(syn_dl_gui.messagebox, name,
                lambda *a, **k: dialogs.append(a) or True)
    syn_dl_gui.simpledialog.askstring = lambda *a, **k: dialogs.append(a)

    tmp = Path(tempfile.mkdtemp())
    # 清掉本机可能存在的会话缓存,避免误连
    import syn_dl
    syn_dl.SESSION_FILE.unlink(missing_ok=True)

    root = tk.Tk()
    root.withdraw()  # 逻辑测试不显示窗口
    app = App(root)
    app.var_host.set(f"http://127.0.0.1:{port}")
    app.var_user.set("alice")
    app.var_pass.set("secret")
    app.var_outdir.set(str(tmp))
    app.var_workers.set(4)

    # 1) 登录
    app.on_login()
    assert pump(root, 10, lambda: app.client is not None), "登录超时"
    assert app.lbl_login.cget("text").startswith("已登录"), \
        f"登录状态异常: {app.lbl_login.cget('text')}"
    print("[gui] 登录 ✔", app.lbl_login.cget("text"))

    # 2) 自动进入根目录(共享文件夹)
    assert pump(root, 10, lambda: getattr(app, "cwd", None) == "/"), "列出共享文件夹超时"
    names = [app.tree.item(i)["text"] for i in app.tree.get_children()]
    assert "media/" in names, f"共享文件夹缺失: {names}"
    print(f"[gui] 浏览 / ✔ → {names}")

    # 3) 进入共享文件夹
    app.load_dir("/media")
    assert pump(root, 10, lambda: getattr(app, "cwd", "") == "/media"), "进入目录超时"
    texts = {app.tree.item(i)["text"]: i for i in app.tree.get_children()}
    mp3_row = texts["Trip Recording Day 01.mp3"]
    print("[gui] 浏览 /media ✔ 找到目标文件")

    # 4) 选中并下载
    app.tree.selection_set(mp3_row)
    app.download_selected()
    assert len(app.tasks) == 1, "任务未创建"
    task = next(iter(app.tasks.values()))
    assert pump(root, 30, lambda: task.finished), "下载超时"
    dest = tmp / "Trip Recording Day 01.mp3"
    got = hashlib.md5(dest.read_bytes()).hexdigest()
    assert got == MD5, f"GUI 下载 md5 不匹配 {got} != {MD5}"
    print(f"[gui] 下载完成 md5 一致 ✔  ({SIZE} B)")
    assert task.status.startswith("完成"), f"任务状态异常: {task.status}"

    # 5) 再次下载同一文件 → 跳过(已存在且无状态文件)
    app.tree.selection_set(mp3_row)
    app.download_selected()
    assert pump(root, 10, lambda: len(app.tasks) == 2), "第二个任务未创建"
    task2 = list(app.tasks.values())[1]
    assert pump(root, 10, lambda: task2.finished)
    assert task2.status.startswith("已存在"), f"重复下载应跳过: {task2.status}"
    print("[gui] 重复下载自动跳过 ✔")

    assert not dialogs, f"出现了意外弹窗: {dialogs}"
    root.destroy()
    srv.shutdown()
    print("\n[gui] 全部通过 ✅")


if __name__ == "__main__":
    main()
