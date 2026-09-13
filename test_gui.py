#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 自动化测试:模拟 DSM,程序化驱动 App 完成登录→浏览→下载,无需人工点击。
会话/凭据全部重定向到临时文件,不触碰真实环境。"""
import hashlib
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tkinter as tk  # noqa: E402

import syn_dl  # noqa: E402
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
    # 隔离真实环境:会话/凭据重定向到临时文件,凭据不写系统钥匙串
    orig_session, orig_cred = syn_dl.SESSION_FILE, syn_dl.CRED_FILE
    orig_save = syn_dl.save_credentials
    syn_dl.SESSION_FILE = tmp / "session.json"
    syn_dl.CRED_FILE = tmp / "cred.json"
    syn_dl.save_credentials = lambda k, a, p, use_keyring=True: orig_save(
        k, a, p, use_keyring=False)

    try:
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
        assert pump(root, 10, lambda: getattr(app, "cwd", None) == "/"), \
            "列出共享文件夹超时"
        names = [app.tree.item(i)["text"] for i in app.tree.get_children()]
        assert "media/" in names, f"共享文件夹缺失: {names}"
        print(f"[gui] 浏览 / ✔ → {names}")

        # 3) 进入共享文件夹
        app.load_dir("/media")
        assert pump(root, 10, lambda: getattr(app, "cwd", "") == "/media"), \
            "进入目录超时"
        texts = {app.tree.item(i)["text"]: i for i in app.tree.get_children()}
        assert "Travel Notes/" in texts, f"子目录缺失: {texts}"
        print("[gui] 浏览 /media ✔")

        # 3.5) 修改日期筛选(/media 层文件:2025-12-31(数值格式)/ 2026-08-01 /
        #      2026-12-25 / 无时间戳;子目录内文件由 3.6 递归搜索覆盖)
        def visible():
            return {app.tree.item(i)["text"] for i in app.tree.get_children()}

        app.var_fmode.set("早于")
        app.var_fdate_a.set("2026-01-01")
        app._render_tree()
        v = visible()
        assert "Old Notes 2025.txt" in v
        assert "readme.txt" not in v and "Future Plan.txt" not in v
        assert "Travel Notes/" in v, "目录不应被筛选掉"
        assert "No Time Stamp.bin" in v, "无时间戳的文件应保留显示"

        app.var_fmode.set("晚于")
        app.var_fdate_a.set("2026-09-01")
        app._render_tree()
        v = visible()
        assert "Future Plan.txt" in v
        assert "readme.txt" not in v and "Old Notes 2025.txt" not in v

        app.var_fmode.set("介于")
        app.var_fdate_a.set("2026-07-01")
        app.var_fdate_b.set("2026-09-30")
        app._render_tree()
        v = visible()
        assert "readme.txt" in v
        assert "Old Notes 2025.txt" not in v and "Future Plan.txt" not in v
        assert "筛选出 2/4" in app.lbl_fhint.cget("text"), app.lbl_fhint.cget("text")

        # 日期 A>B 自动交换
        app.var_fdate_a.set("2026-09-30")
        app.var_fdate_b.set("2026-07-01")
        app._render_tree()
        assert "readme.txt" in visible()

        # 无效日期提示
        app.var_fmode.set("早于")
        app.var_fdate_a.set("not-a-date")
        app._render_tree()
        assert "请输入有效的日期A" in app.lbl_fhint.cget("text")

        # 复原:全部(/media 层 = 1 目录 + 4 文件)
        app.var_fmode.set("全部")
        app._render_tree()
        assert len([i for i in app.tree.get_children()
                    if i != "up"]) == 5, "全部模式下应有 5 项"
        print("[gui] 修改日期筛选(早于/晚于/介于/边界/格式兼容)✔")

        # 3.6) 含子文件夹递归搜索(子目录里的 Day 01/Day 02 应被找到)
        app.var_frecursive.set(True)
        app.var_fmode.set("晚于")
        app.var_fdate_a.set("2026-09-01")
        app._filter_changed()
        assert pump(root, 10, lambda: app._in_search), "递归搜索超时"
        v = visible()
        assert {"Travel Notes/Trip Recording Day 01.mp3",
                "Travel Notes/Day 02.mp3",
                "Future Plan.txt"} <= v, v
        assert not any("readme" in x or "Old Notes" in x for x in v)
        assert "搜索完成:共 4 个文件" in app.lbl_fhint.cget("text"), \
            app.lbl_fhint.cget("text")
        print("[gui] 递归搜索(Search API,相对路径展示)✔")

        # 回退路径:禁用 Search API → 客户端递归遍历,结果一致
        import test_e2e as te
        te.MOCK_STATE["search_enabled"] = False
        app._start_search()
        assert pump(root, 10, lambda: "遍历完成" in app.lbl_fhint.cget("text")), \
            app.lbl_fhint.cget("text")
        te.MOCK_STATE["search_enabled"] = True
        assert "Travel Notes/Day 02.mp3" in visible()
        print("[gui] Search API 不可用时回退为客户端遍历 ✔")

        # 退出搜索视图,恢复浏览
        app.go_up()
        assert pump(root, 10, lambda: not app._in_search
                    and getattr(app, "cwd", "") == "/media"), "退出搜索超时"
        app.var_frecursive.set(False)
        app.var_fmode.set("全部")
        app._filter_changed()
        print("[gui] 退出搜索视图恢复浏览 ✔")

        # 4) 进入子目录选中下载
        app.load_dir("/media/Travel Notes")
        assert pump(root, 10, lambda: getattr(app, "cwd", "") == "/media/Travel Notes")
        texts = {app.tree.item(i)["text"]: i for i in app.tree.get_children()}
        mp3_row = texts["Trip Recording Day 01.mp3"]

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
    finally:
        syn_dl.SESSION_FILE, syn_dl.CRED_FILE = orig_session, orig_cred
        syn_dl.save_credentials = orig_save


if __name__ == "__main__":
    main()
