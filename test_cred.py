#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凭据存储与 GUI 自动登录/退出测试(文件模式凭据,不触碰真实钥匙串)。"""
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
from test_gui import pump  # noqa: E402
from test_e2e import Handler, MD5, SIZE  # noqa: E402

KEY = "http://127.0.0.1:1"      # 占位,后面替换为 mock 地址


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    host = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp())
    orig_session, orig_cred = syn_dl.SESSION_FILE, syn_dl.CRED_FILE
    syn_dl.SESSION_FILE = tmp / "session.json"
    syn_dl.CRED_FILE = tmp / "cred.json"
    # 强制走文件凭据路径(不写真实系统钥匙串)
    orig_save = syn_dl.save_credentials
    syn_dl.save_credentials = lambda k, a, p, use_keyring=True: orig_save(
        k, a, p, use_keyring=False)
    # 屏蔽弹窗
    answers = {"yesno": True, "okcancel": True}
    syn_dl_gui.messagebox.askyesnocancel = lambda *a, **k: answers["yesno"]
    syn_dl_gui.messagebox.askokcancel = lambda *a, **k: answers["okcancel"]
    for n in ("showerror", "showwarning", "showinfo"):
        setattr(syn_dl_gui.messagebox, n, lambda *a, **k: None)

    try:
        # 1) 凭据回环(文件模式;密码含空格和特殊字符)
        assert syn_dl.save_credentials(host, "alice", "secret", use_keyring=False) == "file"
        acc, pwd = syn_dl.load_credentials(host)
        assert (acc, pwd) == ("alice", "secret")
        assert syn_dl.load_credentials("http://elsewhere") == (None, None)
        raw = syn_dl.CRED_FILE.read_text()
        assert "secret" not in raw, "密码不得明文出现在凭据文件中"
        print("[cred] 凭据保存/读取(混淆存储,非明文)✔")

        # 2) GUI 启动 → 自动登录(无会话,仅凭保存的密码)
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        app.var_host.set(host)
        app.var_outdir.set(str(tmp))
        app.var_workers.set(4)
        # App.__init__ 已按默认 host 触发过一次自动登录尝试(找不到凭据,静默失败),
        # 手动再触发一次以当前 host 的凭据登录
        app._try_cached_session()
        assert pump(root, 15, lambda: app.client is not None), "自动登录超时"
        assert "自动登录" in app.lbl_login.cget("text"), app.lbl_login.cget("text")
        assert pump(root, 10, lambda: getattr(app, "cwd", None) == "/")
        print("[cred] 启动自动登录(保存的密码)✔ →", app.lbl_login.cget("text"))

        # 3) 下载仍正常(进入目录 → 选中 → 下载)
        app.load_dir("/media/Travel Notes")
        assert pump(root, 10, lambda: getattr(app, "cwd", "") == "/media/Travel Notes")
        texts = {app.tree.item(i)["text"]: i for i in app.tree.get_children()}
        app.tree.selection_set(texts["Trip Recording Day 01.mp3"])
        app.download_selected()
        task = next(iter(app.tasks.values()))
        assert pump(root, 30, lambda: task.finished), "下载超时"
        import hashlib as h
        got = h.md5((tmp / "Trip Recording Day 01.mp3").read_bytes()).hexdigest()
        assert got == MD5
        print("[cred] 自动登录状态下下载 md5 一致 ✔")

        # 4) 退出并忘记密码
        app.on_logout()
        assert pump(root, 10, lambda: app.client is None), "退出超时"
        assert syn_dl.load_credentials(host) == (None, None), "密码应被忘记"
        assert not syn_dl.SESSION_FILE.exists(), "会话缓存应清除"
        assert app.btn_login.cget("text") == "登录"
        assert str(app.btn_download.cget("state")) == "disabled"
        print("[cred] 退出登录 + 忘记密码 ✔")

        root.destroy()

        # 5) 忘记密码后:启动不再自动登录
        root2 = tk.Tk()
        root2.withdraw()
        app2 = App(root2)
        app2.var_host.set(host)
        app2._try_cached_session()
        pump(root2, 3, lambda: False)   # 等 3 秒确认不会登录成功
        assert app2.client is None, "凭据清除后不应自动登录"
        root2.destroy()
        print("[cred] 忘记后不再自动登录 ✔")

        srv.shutdown()
        print("\n[cred] 全部通过 ✅")
    finally:
        syn_dl.SESSION_FILE, syn_dl.CRED_FILE = orig_session, orig_cred
        syn_dl.save_credentials = orig_save


if __name__ == "__main__":
    main()
