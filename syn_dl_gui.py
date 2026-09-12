#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syn_dl_gui.py — 群晖 DSM 下载客户端 GUI 版(macOS / Windows)

复用 syn_dl.py 的核心(SynClient 登录 + RangeDownloader 多线程断点续传)。
运行:python3 syn_dl_gui.py   (依赖:requests + Python 自带 tkinter)
"""

import queue
import threading
import time
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, simpledialog

from syn_dl import (AUTH_CODES, AbortDownload, AuthExpired, DEFAULT_HOST,
                    RangeDownloader, SynClient, SynError, __version__,
                    clear_credentials, clear_session, human, load_credentials,
                    load_session, save_credentials, save_session)

POLL_MS = 300


class Task:
    """一个下载任务,后台线程跑 RangeDownloader,UI 轮询读取状态字段。"""

    _seq = 0

    def __init__(self, client, remote_path, dest, workers, on_finish):
        Task._seq += 1
        self.id = f"t{Task._seq}"
        self.remote_path = remote_path
        self.dest = Path(dest)
        self.workers = workers
        self.client = client
        self.on_finish = on_finish
        self.status = "连接中…"
        self.done_bytes = 0
        self.size = 0
        self.failures = 0
        self.finished = False
        self.stop_requested = threading.Event()
        self._last_bytes = 0
        self._last_t = time.time()
        self.speed = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        self.status = "正在暂停…"

    # ---- 供 RangeDownloader 的回调(下载线程内执行,只写数字不碰 UI)----
    def _progress(self, done):
        self.done_bytes = done
        now = time.time()
        if now - self._last_t >= 1.0:
            self.speed = (done - self._last_bytes) / (now - self._last_t)
            self._last_bytes, self._last_t = done, now

    def _abort_check(self, _done):
        if self.stop_requested.is_set():
            raise AbortDownload()

    # ---- 主流程 ----
    def _run(self):
        try:
            # 已完整存在的文件直接跳过
            state_file = self.dest.with_name(self.dest.name + ".synstate")
            if self.dest.exists() and not state_file.exists():
                self.status = "已存在,跳过"
                return

            def request_fn(headers):
                return self.client.open_download(self.remote_path, headers)

            dl = RangeDownloader(request_fn, self.dest, workers=self.workers,
                                 reauth=lambda: self.client.ensure_sid(force=True),
                                 progress=self._progress,
                                 abort_check=self._abort_check)
            try:
                dl.run()
            except KeyboardInterrupt:
                self.status = (f"已暂停({human(self.done_bytes)}/"
                               f"{human(max(self.size, 1))},可续传)")
                return
            self.size = dl.size
            self.status = f"完成({human(dl.size)})"
        except AuthExpired as e:
            self.status = f"会话过期:{e}"
        except Exception as e:
            self.status = f"失败:{e.__class__.__name__}: {e}"
        finally:
            self.finished = True
            if self.on_finish:
                self.on_finish(self)


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"群晖下载器 SynDL v{__version__}")
        root.geometry("880x640")
        root.minsize(780, 560)
        self.client = None
        self.tasks = {}
        self.entries = []          # 当前目录条目(与浏览表行号对应)
        self._loading_dir = False
        # 所有后台线程只往此队列丢事件,主线程统一消费——绝不在工作线程碰 tkinter
        self.events = queue.Queue()

        self._build_ui()
        self._try_cached_session()
        root.after(POLL_MS, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- UI 构建
    def _build_ui(self):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        pad = dict(padx=6, pady=4)

        # —— 登录区 ——
        login = ttk.LabelFrame(self.root, text="登录")
        login.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(login, text="服务器/ID:").grid(row=0, column=0, **pad)
        self.var_host = tk.StringVar(value="")
        if (last := load_session(None)):
            self.var_host.set(last.get("qc_id") or last.get("host") or "")
        ttk.Entry(login, textvariable=self.var_host, width=40).grid(row=0, column=1, **pad)
        ttk.Label(login, text="账号:").grid(row=0, column=2, **pad)
        self.var_user = tk.StringVar()
        ttk.Entry(login, textvariable=self.var_user, width=14).grid(row=0, column=3, **pad)
        ttk.Label(login, text="密码:").grid(row=0, column=4, **pad)
        self.var_pass = tk.StringVar()
        ttk.Entry(login, textvariable=self.var_pass, width=14, show="•").grid(
            row=0, column=5, **pad)
        self.btn_login = ttk.Button(login, text="登录", command=self.on_login)
        self.btn_login.grid(row=0, column=6, **pad)
        self.lbl_login = ttk.Label(login, text="未登录", foreground="#888")
        self.lbl_login.grid(row=0, column=7, **pad)
        self.var_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(login, text="记住密码(下次自动登录)",
                        variable=self.var_remember).grid(row=1, column=1,
                                                         columnspan=3, sticky="w")

        # —— 浏览区 ——
        browse = ttk.LabelFrame(self.root, text="远程文件")
        browse.pack(fill="both", expand=True, padx=10, pady=4)
        bar = ttk.Frame(browse)
        bar.pack(fill="x", padx=6, pady=(4, 0))
        self.btn_up = ttk.Button(bar, text="↑ 上级", command=self.go_up, state="disabled")
        self.btn_up.pack(side="left")
        self.btn_refresh = ttk.Button(bar, text="刷新", command=self.refresh_dir,
                                      state="disabled")
        self.btn_refresh.pack(side="left", padx=6)
        self.lbl_path = ttk.Label(bar, text="(登录后显示共享文件夹)", foreground="#555")
        self.lbl_path.pack(side="left", padx=8)

        # —— 日期筛选行 ——
        fbar = ttk.Frame(browse)
        fbar.pack(fill="x", padx=6, pady=(2, 0))
        ttk.Label(fbar, text="修改日期筛选:").pack(side="left")
        self.var_fmode = tk.StringVar(value="全部")
        self.cmb_fmode = ttk.Combobox(
            fbar, textvariable=self.var_fmode, state="readonly", width=8,
            values=("全部", "早于", "晚于", "介于"))
        self.cmb_fmode.pack(side="left", padx=(2, 8))
        ttk.Label(fbar, text="日期A:").pack(side="left")
        self.var_fdate_a = tk.StringVar()
        e_a = ttk.Entry(fbar, textvariable=self.var_fdate_a, width=11)
        e_a.pack(side="left", padx=(2, 8))
        ttk.Label(fbar, text="日期B:").pack(side="left")
        self.var_fdate_b = tk.StringVar()
        e_b = ttk.Entry(fbar, textvariable=self.var_fdate_b, width=11)
        e_b.pack(side="left", padx=2)
        self.lbl_fhint = ttk.Label(fbar, text="格式 YYYY-MM-DD", foreground="#888")
        self.lbl_fhint.pack(side="left", padx=10)
        self.cmb_fmode.bind("<<ComboboxSelected>>", lambda _e: self._render_tree())
        for ent in (e_a, e_b):
            ent.bind("<KeyRelease>", lambda _e: self._render_tree())

        cols = ("type", "size")
        self.tree = ttk.Treeview(browse, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="名称")
        self.tree.heading("type", text="类型")
        self.tree.heading("size", text="大小")
        self.tree.column("#0", width=430, anchor="w")
        self.tree.column("type", width=80, anchor="center")
        self.tree.column("size", width=100, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.tree.bind("<Double-1>", self.on_double_click)

        # —— 下载设置 ——
        opts = ttk.Frame(self.root)
        opts.pack(fill="x", padx=10, pady=2)
        ttk.Label(opts, text="保存到:").pack(side="left")
        self.var_outdir = tk.StringVar(value=str(Path.home() / "Downloads"))
        ttk.Entry(opts, textvariable=self.var_outdir, width=52).pack(side="left", padx=4)
        ttk.Button(opts, text="浏览…", command=self.choose_outdir).pack(side="left")
        ttk.Label(opts, text="  线程:").pack(side="left")
        self.var_workers = tk.IntVar(value=8)
        ttk.Spinbox(opts, from_=1, to=32, textvariable=self.var_workers, width=4).pack(side="left")
        self.btn_download = ttk.Button(opts, text="↓ 下载选中", command=self.download_selected,
                                       state="disabled")
        self.btn_download.pack(side="left", padx=12)

        # —— 任务区 ——
        tasks = ttk.LabelFrame(self.root, text="下载任务(断点续传:暂停/关掉程序后,重新下载同一文件会自动继续)")
        tasks.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        tcols = ("bar", "speed", "status")
        self.task_tree = ttk.Treeview(tasks, columns=tcols, height=6)
        self.task_tree.heading("#0", text="文件")
        self.task_tree.heading("bar", text="进度")
        self.task_tree.heading("speed", text="速度")
        self.task_tree.heading("status", text="状态")
        self.task_tree.column("#0", width=330, anchor="w")
        self.task_tree.column("bar", width=215, anchor="w")
        self.task_tree.column("speed", width=90, anchor="e")
        self.task_tree.column("status", width=200, anchor="w")
        self.task_tree.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Button(tasks, text="暂停选中任务", command=self.pause_selected).pack(
            side="right", padx=6, pady=(0, 10))

    # ---------------------------------------------------------------- 登录
    def _try_cached_session(self):
        """启动时自动登录:优先验证缓存会话,失效则用保存的密码重登。"""
        raw = self.var_host.get().strip()
        cached = load_session(raw)

        def worker():
            client = None
            ok = False
            # 1) 缓存会话直接验证(免 QuickConnect 定位往返)
            if cached:
                client = SynClient(cached.get("host") or raw)
                client.qc_id = cached.get("qc_id") or client.qc_id
                try:
                    ok = client.try_sid(cached["sid"])
                except Exception:
                    ok = False
            # 2) 会话失效/不存在 → 用保存的密码自动登录
            if not ok:
                client = SynClient(raw)
                cred_key = client.qc_id or client.raw_host
                saved_acc, saved_pwd = load_credentials(cred_key)
                if saved_pwd:
                    try:
                        client.discover()      # QuickConnect ID 在此解析
                        client.login(saved_acc, saved_pwd)
                        save_session(client)
                        ok = True
                    except Exception:
                        ok = False
            if not ok and client is not None:
                client = None
            self.events.put(("cache_result", client, ok, cached))

        threading.Thread(target=worker, daemon=True).start()

    def _after_cache(self, client, ok, cached):
        if ok and client is not None:
            self.client = client
            if not cached:
                client.account = load_credentials(client.qc_id or client.raw_host)[0] or ""
            self.var_user.set(client.account or "")
            where = f" @ {client.host.split('://')[-1]}" if client.qc_id else ""
            src = "缓存会话" if cached else "保存的密码"
            self._mark_logged_in(f"已自动登录({src}):{client.account}{where}")
            self.load_dir("/")
        # 失败则静默,等用户手动登录

    def on_login(self):
        if self.client:
            return
        host = self.var_host.get().strip()
        account = self.var_user.get().strip()
        password = self.var_pass.get()
        if not (host and account and password):
            messagebox.showwarning("提示", "请填写服务器、账号和密码")
            return
        self.btn_login.config(state="disabled")
        self.lbl_login.config(text="登录中…", foreground="#888")
        self.var_pass.set("")

        def worker():
            client = SynClient(host)
            err = None
            try:
                client.discover()          # QuickConnect ID 在此解析为中继地址
                try:
                    client.login(account, password)
                except SynError as e:
                    if "OTP" in str(e) or "验证码" in str(e):
                        self.events.put(("otp_needed", client, account, password))
                        return
                    raise
            except Exception as e:
                err = f"{e.__class__.__name__}: {e}"
            self.events.put(("login_result", client, err, password))

        threading.Thread(target=worker, daemon=True).start()

    def _ask_otp(self, client, account, password):
        otp = simpledialog.askstring("两步验证", "请输入一次性验证码(OTP):", parent=self.root)
        if not otp:
            self._after_login(client, "已取消", password)
            return

        def worker():
            err = None
            try:
                client.login(account, password, otp)
            except Exception as e:
                err = f"{e.__class__.__name__}: {e}"
            self.events.put(("login_result", client, err, password))

        threading.Thread(target=worker, daemon=True).start()

    def _after_login(self, client, err, password=None):
        self.btn_login.config(state="normal")
        if err:
            self.lbl_login.config(text=f"登录失败:{err}", foreground="#c0392b")
            messagebox.showerror("登录失败", err)
            return
        self.client = client
        try:
            save_session(client)
        except Exception:
            pass
        if password and self.var_remember.get():
            try:
                kind = save_credentials(client.qc_id or client.raw_host,
                                        client.account, password)
                if kind == "file":
                    self.lbl_login.config(
                        text="⚠ 系统钥匙串不可用,密码以本机文件保存(仅混淆)")
            except Exception:
                pass
        where = f" @ {client.host.split('://')[-1]}" if client.qc_id else ""
        self._mark_logged_in(f"已登录:{client.account}{where}")
        self.load_dir("/")

    def _mark_logged_in(self, text):
        self.lbl_login.config(text=text, foreground="#1e8449")
        self.btn_login.config(text="退出", command=self.on_logout)
        self.btn_up.config(state="normal")
        self.btn_refresh.config(state="normal")
        self.btn_download.config(state="normal")

    def _mark_logged_out(self, text="未登录"):
        self.client = None
        self.lbl_login.config(text=text, foreground="#888")
        self.btn_login.config(text="登录", command=self.on_login, state="normal")
        self.btn_up.config(state="disabled")
        self.btn_refresh.config(state="disabled")
        self.btn_download.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.lbl_path.config(text="(登录后显示共享文件夹)")

    def on_logout(self):
        running = [t for t in self.tasks.values() if not t.finished]
        if running and not messagebox.askokcancel(
                "退出登录", f"有 {len(running)} 个任务仍在下载,退出后这些任务会失败。\n"
                           f"进度会保留(可重新登录后续传)。仍要退出?"):
            return
        ans = messagebox.askyesnocancel(
            "退出登录", "是否同时忘记已保存的密码?\n\n"
                       "「是」= 忘记密码,下次手动登录\n"
                       "「否」= 保留密码,下次自动登录\n"
                       "「取消」= 不退出")
        if ans is None:
            return
        client = self.client
        forget = ans

        def worker():
            cred_key = client.qc_id or client.raw_host
            try:
                client.logout()
            except Exception:
                pass
            clear_session()
            if forget:
                clear_credentials(cred_key)
            self.events.put(("logged_out", bool(forget)))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------------- 目录浏览
    def load_dir(self, path):
        if not self.client or self._loading_dir:
            return
        self._loading_dir = True
        self.lbl_path.config(text=f"{path}  (加载中…)")
        client = self.client

        def worker():
            try:
                if path == "/":
                    entries = client.list_shares()
                else:
                    entries = client.list_dir(path)
                err = None
            except AuthExpired:
                # 尝试自动重登一次
                try:
                    client.ensure_sid(force=True)
                    entries = client.list_shares() if path == "/" else client.list_dir(path)
                    err = None
                except Exception as e:
                    entries, err = None, f"{e.__class__.__name__}: {e}"
            except Exception as e:
                entries, err = None, f"{e.__class__.__name__}: {e}"
            self.events.put(("dir_loaded", path, entries, err))

        threading.Thread(target=worker, daemon=True).start()

    def _fill_dir(self, path, entries, err):
        self._loading_dir = False
        if err:
            self.lbl_path.config(text=path or "/")
            messagebox.showerror("读取目录失败", err)
            return
        self.cwd = path
        self.entries = entries
        self.lbl_path.config(text=path)
        self._render_tree()

    # ---------------------------------------------------------------- 日期筛选与渲染
    def _render_tree(self):
        """按当前筛选条件把 self.entries 渲染到浏览表(iid → entry 映射)。"""
        self.tree.delete(*self.tree.get_children())
        self._row_entry = {}
        if getattr(self, "cwd", "/") != "/":
            self.tree.insert("", "end", iid="up", text="..(上级)", values=("上级", ""))

        mode = self.var_fmode.get()
        start = end = None
        hint = ""
        if mode != "全部":
            start = self._parse_date(self.var_fdate_a.get())
            end = self._parse_date(self.var_fdate_b.get()) if mode == "介于" else None
            if mode == "介于":
                if start is None or end is None:
                    hint = "请输入有效的日期A和日期B"
                elif start > end:
                    hint = "日期A晚于日期B,已自动交换"
                    start, end = end, start
            elif start is None:
                hint = "请输入有效的日期A"
        if hint.startswith("请输入"):
            mode = "全部"   # 日期无效时不做筛选,仅显示提示

        shown = total = 0
        for e in self.entries:
            keep = True
            if not e["isdir"]:
                total += 1
                keep = self._match_date(e.get("mtime"), mode, start, end)
                if keep:
                    shown += 1
            if not keep:
                continue
            name = e["name"] + ("/" if e["isdir"] else "")
            size = human(e["size"]) if e.get("size") else ""
            mtime = date.fromtimestamp(e["mtime"]).isoformat() if e.get("mtime") else ""
            iid = self.tree.insert(
                "", "end", values=("文件夹" if e["isdir"] else "文件",
                                   f"{size}  {mtime}" if mtime else size),
                text=name)
            self._row_entry[iid] = e

        if hint:
            color = "#c0392b" if hint.startswith("请输入") else "#888"
            self.lbl_fhint.config(text=hint, foreground=color)
        elif mode == "全部":
            self.lbl_fhint.config(text="格式 YYYY-MM-DD", foreground="#888")
        else:
            self.lbl_fhint.config(text=f"筛选出 {shown}/{total} 个文件",
                                  foreground="#1e8449")

    @staticmethod
    def _parse_date(s):
        try:
            return date.fromisoformat(s.strip())
        except Exception:
            return None

    @staticmethod
    def _match_date(mtime, mode, start, end):
        if mode == "全部" or not mtime:
            return True
        try:
            d = date.fromtimestamp(mtime)
        except Exception:
            return True
        if mode == "早于":
            return d < start
        if mode == "晚于":
            return d > start
        return start <= d <= end   # 介于(含两端)

    def refresh_dir(self):
        self.load_dir(getattr(self, "cwd", "/"))

    def go_up(self):
        cwd = getattr(self, "cwd", "/")
        if cwd == "/":
            return
        self.load_dir(cwd.rsplit("/", 1)[0] or "/")

    def on_double_click(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == "up":
            self.go_up()
            return
        entry = getattr(self, "_row_entry", {}).get(iid)
        if not entry:
            return
        if entry["isdir"]:
            self.load_dir(entry["path"])
        else:
            self._download_paths([entry])

    # ---------------------------------------------------------------- 下载
    def choose_outdir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or str(Path.home()))
        if d:
            self.var_outdir.set(d)

    def download_selected(self):
        sel = self.tree.selection()
        paths = []
        for iid in sel:
            if iid == "up":
                continue
            entry = getattr(self, "_row_entry", {}).get(iid)
            if entry and not entry["isdir"]:
                paths.append(entry)
        if not paths:
            messagebox.showinfo("提示", "请先选中要下载的文件(可按住 Ctrl/Cmd 多选)")
            return
        self._download_paths(paths)

    def _download_paths(self, entries):
        outdir = Path(self.var_outdir.get().strip() or Path.home() / "Downloads")
        outdir.mkdir(parents=True, exist_ok=True)
        workers = int(self.var_workers.get())
        started = 0
        for e in entries:
            dest = outdir / e["name"]
            if any(t.remote_path == e["path"] and not t.finished for t in self.tasks.values()):
                continue  # 同一文件已在任务列表,跳过
            task = Task(self.client, e["path"], dest, workers,
                        on_finish=lambda t: None)
            self.tasks[task.id] = task
            self.task_tree.insert("", "end", iid=task.id, text=e["name"],
                                  values=("等待中", "", "排队"))
            task.start()
            started += 1
        if not started:
            messagebox.showinfo("提示", "所选文件都已在下载列表中")

    def pause_selected(self):
        for iid in self.task_tree.selection():
            t = self.tasks.get(iid)
            if t and not t.finished:
                t.stop()

    # ---------------------------------------------------------------- 轮询刷新(主线程唯一入口)
    def _poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "login_result":
                    self._after_login(ev[1], ev[2], ev[3])
                elif kind == "otp_needed":
                    self._ask_otp(ev[1], ev[2], ev[3])
                elif kind == "cache_result":
                    self._after_cache(ev[1], ev[2], ev[3])
                elif kind == "dir_loaded":
                    self._fill_dir(ev[1], ev[2], ev[3])
                elif kind == "logged_out":
                    self._mark_logged_out("已退出登录" + ("(密码已忘记)" if ev[1]
                                                           else "(密码已保留)"))
        except queue.Empty:
            pass
        self._update_task_rows()
        self.root.after(POLL_MS, self._poll)

    def _update_task_rows(self):
        for tid, t in self.tasks.items():
            pct = (t.done_bytes * 100 / t.size) if t.size else 0
            if t.finished or t.status.startswith(("完成", "已存在", "已暂停", "失败", "会话")):
                bar_txt = f"{pct:.1f}%"
                speed, status = "", t.status
            else:
                filled = int(pct / 4)
                bar_txt = "█" * filled + "░" * (25 - filled) + f" {pct:5.1f}%"
                speed = f"{human(t.speed)}/s" if t.speed > 1 else ""
                status = t.status
                if t.failures:
                    status += f"(重试{t.failures})"
            try:
                self.task_tree.item(tid, values=(bar_txt, speed, status))
            except tk.TclError:
                pass

    def _on_close(self):
        running = [t for t in self.tasks.values() if not t.finished]
        if running and not messagebox.askokcancel(
                "退出", f"还有 {len(running)} 个任务正在下载。\n"
                        f"退出会暂停任务(进度已保存,重新下载同一文件可续传)。确定退出?"):
            return
        for t in running:
            t.stop()
        deadline = time.time() + 2
        for t in running:
            t.thread.join(max(0, deadline - time.time()))
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
