#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syn_dl.py — 群晖 DSM 文件下载客户端(登录 + 浏览 + 多线程断点续传)

普通用户权限即可(只需 File Station 访问权),无需管理员。

用法:
  交互模式:   python3 syn_dl.py
  直接下载:   python3 syn_dl.py --get "/media/My Trip 2026/xxx.mp3"
  列目录:     python3 syn_dl.py --ls "/media"

常用参数:
  --host URL      DSM 地址(默认 QuickConnect 域名;内网直连可大幅提速)
  -u, --user      账号(不填则交互询问)
  -w, --workers   并发线程数(默认 8)
  -o, --out       保存位置(目录或文件名)
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

try:
    import readline  # noqa: F401  启用 input() 的行编辑与路径补全
except ImportError:
    pass

__version__ = "0.3.1"
DEFAULT_HOST = ""                     # 留空:首次使用时询问,并记住上次地址
SESSION_FILE = Path.home() / ".syn_dl_session.json"
UA = "syn-dl/1.0"
CHUNK_SIZE = 16 * 1024 * 1024          # 每块 16MB
AUTH_CODES = {105, 119, 401, 403}      # DSM 会话失效/未授权错误码


def looks_like_qc_id(s):
    """不含点号和协议头的输入视为 QuickConnect ID(如 my-nas-id)。"""
    s = s.strip()
    return bool(s) and "://" not in s and "." not in s and "/" not in s


QC_REGIONS = ["de", "us", "tw", "cnc", "uk", "fr", "jp", "sg", "au", "hk"]


def resolve_quickconnect(qc_id, entry_base=None):
    """
    通过 QuickConnect 区域入口定位 NAS 的实际可访问地址。
    实测机制:{id}.{区域}.quickconnect.to 会 307 到该 NAS 注册的中继
    (如 {id}.{region}.quickconnect.to);不同区域入口殊途同归,并行探测取最快可达者。
    返回形如 https://{id}.{regionN}.quickconnect.to 的基址。
    """
    if entry_base:                       # 测试注入单一入口
        candidates = [entry_base]
    else:
        candidates = [f"https://{qc_id}.{r}.quickconnect.to" for r in QC_REGIONS]

    def probe(entry):
        r = requests.get(
            f"{entry}/webapi/query.cgi",
            params={
                "api": "SYNO.API.Info", "version": 1, "method": "query",
                "query": "SYNO.API.Auth,SYNO.FileStation.List,SYNO.FileStation.Download",
            },
            timeout=(5, 15), headers={"User-Agent": UA}, allow_redirects=True,
        )
        j = r.json()
        if not j.get("success"):
            raise SynError(f"入口 {entry} 返回异常: {j}")
        return r.url.split("/webapi/")[0]

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        futures = [pool.submit(probe, c) for c in candidates]
        for fut in as_completed(futures):
            try:
                return fut.result()
            except Exception:
                continue
    raise SynError(f"QuickConnect ID “{qc_id}” 无法定位:所有区域入口均不可达,"
                   f"请检查网络或改用完整地址(如 https://{qc_id}.{region}.quickconnect.to)")


def human(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024


class SynError(Exception):
    pass


class AuthExpired(SynError):
    pass


class AbortDownload(Exception):
    """内部:请求中止当前下载(用于测试钩子)。"""


# ---------------------------------------------------------------- DSM API 客户端
def _normalize_mtime(add):
    """
    归一化 File Station 的修改时间。真实 DSM 7 返回:
      "additional": {"time": {"mtime": "2026-09-10 12:00:00", ...}}
    亦有数值(unix 秒)或纯字符串形态。统一为 'YYYY-MM-DD' 字符串
    (按天粒度;字符串日期无时区换算问题,unix 秒按本机时区取日期)。
    """
    import datetime
    t = add.get("time")
    if isinstance(t, dict):
        t = t.get("mtime")
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return datetime.datetime.fromtimestamp(t).date().isoformat()
    s = str(t).strip()
    return s[:10] if len(s) >= 10 else (s or None)


class SynClient:
    def __init__(self, host, timeout=(10, 60)):
        raw = host.strip().rstrip("/")
        if "://" not in raw:
            raw = "https://" + raw
        self.qc_id = raw.split("://", 1)[1].lower() if looks_like_qc_id(raw.split("://", 1)[1]) else None
        self.raw_host = raw
        self.host = raw            # 若为 QuickConnect ID,discover() 时会更新为中继地址
        self.timeout = timeout
        self.sid = None
        self.account = None
        self._password = None            # 仅内存保存,用于 SID 过期后自动重登
        self._login_lock = threading.Lock()
        self._entry = "/webapi/entry.cgi"

    # ---------- 登录 ----------
    def discover(self, _entry_override=None):
        if self.qc_id:
            self.host = resolve_quickconnect(self.qc_id, _entry_override)
        r = requests.get(
            f"{self.host}/webapi/query.cgi",
            params={
                "api": "SYNO.API.Info", "version": 1, "method": "query",
                "query": "SYNO.API.Auth,SYNO.FileStation.List,SYNO.FileStation.Download",
            },
            timeout=(10, 30), headers={"User-Agent": UA},
        )
        r.raise_for_status()
        info = r.json()
        if not info.get("success"):
            raise SynError(f"API 信息查询失败: {info}")
        return info.get("data", {})

    def login(self, account, password, otp=None):
        r = requests.get(
            f"{self.host}{self._entry}",
            params=self._auth_params(account, password, otp),
            timeout=(10, 30), headers={"User-Agent": UA},
        )
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            err = j.get("error", {}).get("code")
            hint = {400: "账号或密码错误", 401: "账号或密码错误", 402: "账号被停用/权限不足",
                    403: "需要一次性验证码(OTP)或验证码错误", 404: "账号被停用"}.get(err, "登录失败")
            raise SynError(f"登录失败(错误码 {err}):{hint}")
        self.sid = j["data"]["sid"]
        self.account = account
        self._password = password
        return self.sid

    @staticmethod
    def _auth_params(account, password, otp):
        p = {"api": "SYNO.API.Auth", "version": "3", "method": "login",
             "account": account, "passwd": password,
             "session": "FileStation", "format": "sid"}
        if otp:
            p["otp_code"] = otp
        return p

    # ---------- 会话管理 ----------
    def try_sid(self, sid):
        """验证缓存 SID 是否仍有效。"""
        self.sid = sid
        try:
            self.list_shares()
            return True
        except Exception:
            self.sid = None
            return False

    def logout(self):
        """注销当前会话(尽力而为,失败不影响本地清理)。"""
        if not self.sid:
            return
        try:
            requests.get(
                f"{self.host}{self._entry}",
                params={"api": "SYNO.API.Auth", "version": "3",
                        "method": "logout", "_sid": self.sid},
                timeout=(10, 30), headers={"User-Agent": UA},
            )
        except Exception:
            pass
        self.sid = None
        self._password = None

    def ensure_sid(self, force=False):
        """force=True 时强制重新登录(SID 过期后的 reauth 回调)。"""
        with self._login_lock:
            if not force and self.sid:
                return self.sid
            if self._password:
                self.login(self.account, self._password)
                return self.sid
            raise AuthExpired("会话已过期,需要重新登录(未保存密码,无法自动重登)")

    # ---------- File Station ----------
    def _api(self, params, timeout=None):
        params = dict(params)
        params["_sid"] = self.sid
        r = requests.get(f"{self.host}{self._entry}", params=params,
                         timeout=timeout or self.timeout, headers={"User-Agent": UA})
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            code = j.get("error", {}).get("code")
            if code in AUTH_CODES:
                raise AuthExpired(f"会话失效(错误码 {code})")
            raise SynError(f"API 调用失败: {j.get('error')}")
        return j.get("data", {})

    def list_shares(self):
        data = self._api({"api": "SYNO.FileStation.List", "version": "2",
                          "method": "list_share", "additional": '["name"]'})
        out = []
        for s in data.get("shares", []):
            out.append({"name": s.get("name", ""), "path": s["path"],
                        "isdir": True, "size": None})
        return out

    def list_dir(self, path):
        data = self._api({"api": "SYNO.FileStation.List", "version": "2",
                          "method": "list", "folder_path": json.dumps(path),
                          "additional": '["size","time"]', "limit": 0, "sort_by": "name",
                          "sort_direction": "asc"})
        out = []
        for f in data.get("files", []):
            add = f.get("additional", {})
            out.append({"name": f.get("name", ""), "path": f["path"],
                        "isdir": bool(f.get("isdir")),
                        "size": add.get("size"),
                        "mtime": _normalize_mtime(add)})   # 'YYYY-MM-DD' 或 None
        return out

    # ---------- 递归搜索 ----------
    def search_by_time(self, folder, date_from=None, date_to=None):
        """
        在 folder(含全部子目录)按修改时间递归搜索文件(服务端 Search API)。
        date_from/date_to:'YYYY-MM-DD',均为含当天的边界。
        返回文件条目列表(name/path/isdir/size/mtime)。
        """
        params = {"api": "SYNO.FileStation.Search", "version": "2",
                  "method": "start", "folder_path": json.dumps(folder),
                  "pattern": "*", "filetype": "file"}
        if date_from:
            params["mtime_from"] = int(time.mktime(
                time.strptime(date_from, "%Y-%m-%d")))
        if date_to:
            params["mtime_to"] = int(time.mktime(
                time.strptime(date_to + " 23:59:59", "%Y-%m-%d %H:%M:%S")))
        taskid = self._api(params)["taskid"]
        out, offset, pages = [], 0, 0
        try:
            while pages < 200:                     # 防御:最多取 200 页
                pages += 1
                data = self._api({"api": "SYNO.FileStation.Search", "version": "2",
                                  "method": "list", "taskid": taskid, "offset": offset,
                                  "limit": 1000, "sort_by": "name",
                                  "sort_direction": "asc", "filetype": "file",
                                  "additional": '["size","time"]'})
                for f in data.get("files", []):
                    add = f.get("additional", {})
                    out.append({"name": f.get("name", ""), "path": f["path"],
                                "isdir": False, "size": add.get("size"),
                                "mtime": _normalize_mtime(add)})
                offset += len(data.get("files", []))
                total = data.get("total")
                if data.get("finished") or (total is not None and offset >= total) \
                        or not data.get("files"):
                    break
            return out
        finally:
            try:
                self._api({"api": "SYNO.FileStation.Search", "version": "2",
                           "method": "stop", "taskid": taskid})
            except Exception:
                pass

    def walk_files(self, folder, date_from=None, date_to=None):
        """Search API 不可用时的回退:客户端递归遍历(广度优先,4 并发)。"""
        def ok(mdate):
            if not mdate:
                return True
            if date_from and mdate < date_from:
                return False
            if date_to and mdate > date_to:
                return False
            return True

        results, dirs = [], [folder]
        with ThreadPoolExecutor(max_workers=4) as pool:
            while dirs:
                batch, dirs = dirs, []
                for entries in pool.map(self.list_dir, batch):
                    for e in entries:
                        if e["isdir"]:
                            dirs.append(e["path"])
                        elif ok(e.get("mtime")):
                            results.append(e)
        return results

    def open_download(self, path, extra_headers=None):
        """发起文件下载请求(流式)。返回 requests.Response。"""
        url = f"{self.host}{self._entry}"
        params = {"api": "SYNO.FileStation.Download", "version": "2",
                  "method": "download", "path": json.dumps(path),
                  "mode": "download", "_sid": self.sid}
        headers = {"User-Agent": UA}
        if extra_headers:
            headers.update(extra_headers)
        return requests.get(url, params=params, headers=headers,
                            stream=True, timeout=self.timeout)


def check_syn_error(resp):
    """DSM 出错时可能返回 200 + JSON,统一识别。"""
    ct = resp.headers.get("content-type", "")
    if "json" in ct.lower():
        try:
            body = resp.json()
        except Exception:
            raise SynError(f"服务器返回 JSON 错误响应(HTTP {resp.status_code})")
        code = body.get("error", {}).get("code")
        if code in AUTH_CODES:
            raise AuthExpired(f"会话失效(错误码 {code})")
        raise SynError(f"服务器错误响应: {body}")


# ---------------------------------------------------------------- 多线程断点续传核心
class RangeDownloader:
    """
    request_fn(headers) -> requests.Response(stream=True)
        每次请求一个字节区间;由调用方处理认证(SID)。
    progress(bytes_done) 定期回调。
    """

    def __init__(self, request_fn, dest, workers=8, chunk=CHUNK_SIZE,
                 reauth=None, progress=None, abort_check=None):
        self.request_fn = request_fn
        self.dest = Path(dest)
        self.workers = workers
        self.chunk = chunk
        self.reauth = reauth or (lambda: None)
        self.progress = progress
        self.abort_check = abort_check      # 测试钩子:每批写入后调用
        self.size = 0
        self.ranged = True
        self.done_bytes = 0
        self._base_bytes = 0                     # 已完成块字节合计
        self._chunk_local = {}                   # 进行中各块的当前块内偏移
        self._stop = threading.Event()
        self._finished = threading.Event()      # 下载已结束,通知 saver 线程退出
        self._saver_thread = None
        self._state_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._fd = None
        self.failures = 0

    # ---------- 探测 ----------
    def probe(self):
        resp = self.request_fn({"Range": "bytes=0-0"})
        with resp:
            check_syn_error(resp)
            if resp.status_code == 206:
                m = re.search(r"/(\d+)\s*$", resp.headers.get("content-range", ""))
                if not m:
                    raise SynError("无法解析 Content-Range")
                self.size = int(m.group(1))
                self.ranged = True
            elif resp.status_code == 200:
                self.size = int(resp.headers.get("content-length", 0))
                self.ranged = False
            elif resp.status_code in (401, 403):
                raise AuthExpired(f"HTTP {resp.status_code}:无权下载")
            else:
                raise SynError(f"探测失败:HTTP {resp.status_code}")

    # ---------- 状态文件 ----------
    @property
    def state_file(self):
        return self.dest.with_name(self.dest.name + ".synstate")

    def _load_state(self):
        if not self.state_file.exists():
            return None
        try:
            st = json.loads(self.state_file.read_text())
            if (st.get("size") == self.size and st.get("chunk") == self.chunk
                    and len(st.get("done", [])) == self._nchunks()):
                return st
        except Exception:
            pass
        return None

    def _save_state(self):
        with self._state_lock:
            st = {"size": self.size, "chunk": self.chunk,
                  "done": list(self.done), "updated": time.time()}
            tmp = self.state_file.with_suffix(".synstate.tmp")
            tmp.write_text(json.dumps(st))
            tmp.replace(self.state_file)

    def _nchunks(self):
        return (self.size + self.chunk - 1) // self.chunk if self.size else 0

    def _update_progress(self, idx, local_bytes):
        """全局进度 = 已完成块字节 + 各进行中块的块内偏移(重试块先清零)。"""
        with self._counter_lock:
            self._chunk_local[idx] = local_bytes
            self.done_bytes = self._base_bytes + sum(self._chunk_local.values())

    # ---------- 执行 ----------
    def run(self):
        # 探测阶段也可能遇到会话过期,带重登重试
        for attempt in range(3):
            try:
                self.probe()
                break
            except AuthExpired:
                if attempt == 2:
                    raise
                self.reauth()  # 无法重登时会抛出,由调用方提示
        if self.size <= 0:
            raise SynError("服务器未返回文件大小,无法下载")

        st = self._load_state()
        self.done = [False] * self._nchunks()
        if st:
            self.done = st["done"]
            self.done_bytes = sum(self.chunk if i < len(self.done) - 1 else
                                  self.size - i * self.chunk
                                  for i, ok in enumerate(self.done) if ok)
            print(f"↩ 检测到断点记录:从 {human(self.done_bytes)} / {human(self.size)}"
                  f"({self.done_bytes * 100 // self.size}%)继续")
        self._base_bytes = self.done_bytes

        # 预分配目标文件
        if not self.dest.exists() or self.dest.stat().st_size != self.size:
            with open(self.dest, "wb"):
                pass
            os.truncate(self.dest, self.size)
        self._fd = os.open(self.dest, os.O_RDWR)

        try:
            if self.ranged:
                self._run_parallel()
            else:
                self._run_single()
            if self._stop.is_set():
                self._save_state()
                raise KeyboardInterrupt
            final = os.fstat(self._fd).st_size
            if final != self.size:
                raise SynError(f"完整性校验失败:期望 {self.size},实际 {final} 字节")
        finally:
            os.close(self._fd)
            if self._stop.is_set():
                self._save_state()

        self._join_saver()                       # 双保险:确保没有任何线程还会写状态
        self.state_file.unlink(missing_ok=True)
        return self.size

    # ---------- 多线程模式 ----------
    def _run_parallel(self):
        self._saver_thread = threading.Thread(target=self._saver_loop, daemon=True)
        self._saver_thread.start()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._download_chunk, i)
                       for i in range(len(self.done))]
            for f in futures:
                f.result()
        # 先等 saver 线程退出再继续,否则它可能在状态文件被删除后把文件写回来
        self._join_saver()
        self._save_state()

    def _saver_loop(self):
        while not (self._stop.is_set() or self._finished.is_set()):
            time.sleep(3)
            if not (self._stop.is_set() or self._finished.is_set()):
                self._save_state()

    def _join_saver(self):
        self._finished.set()
        if self._saver_thread:
            self._saver_thread.join(5)

    def _download_chunk(self, idx):
        if self.done[idx]:
            return
        start = idx * self.chunk
        end = min(start + self.chunk, self.size) - 1
        attempt = 0
        while not self._stop.is_set():
            try:
                resp = self.request_fn({"Range": f"bytes={start}-{end}"})
                with resp:
                    check_syn_error(resp)
                    if resp.status_code != 206:
                        raise SynError(f"分块 {idx} 收到 HTTP {resp.status_code}(预期 206)")
                    cr = resp.headers.get("content-range", "")
                    if not cr.startswith(f"bytes {start}-"):
                        raise SynError(f"分块 {idx} 返回区间不匹配: {cr!r}")
                    off = start
                    self._update_progress(idx, 0)   # 重试本块时先清零局部进度,避免重复计数
                    for data in resp.iter_content(256 * 1024):
                        os.pwrite(self._fd, data, off)
                        off += len(data)
                        self._update_progress(idx, off - start)
                        if self.progress:
                            self.progress(self.done_bytes)
                        if self.abort_check:
                            self.abort_check(self.done_bytes)
                    if off != end + 1:
                        raise SynError(f"分块 {idx} 数据不完整({off - start}/{end - start + 1})")
                chunk_len = end - start + 1
                with self._counter_lock:
                    self._chunk_local.pop(idx, None)
                    self._base_bytes += chunk_len
                    self.done_bytes = self._base_bytes + sum(self._chunk_local.values())
                self.done[idx] = True
                self._save_state()
                return
            except AuthExpired:
                try:
                    self.reauth()
                    continue          # 立即用新 SID 重试本块
                except Exception as e:
                    self._stop.set()
                    print(f"\n✗ 会话过期且无法自动重登:{e}", file=sys.stderr)
                    raise
            except KeyboardInterrupt:
                self._stop.set()
                return
            except AbortDownload:
                self._stop.set()
                return
            except Exception as e:
                attempt += 1
                self.failures += 1
                wait = min(30, 2 ** min(attempt, 5))
                print(f"\n⚠ 分块 {idx} 第 {attempt} 次失败({e.__class__.__name__}: {e}),"
                      f"{wait}s 后重试", file=sys.stderr)
                time.sleep(wait)

    # ---------- 单流模式(服务器不支持 Range 时兜底) ----------
    def _run_single(self):
        print("ℹ 服务器不支持 Range 请求,使用单线程模式(无法分块续传)")
        resp = self.request_fn({})
        with resp:
            check_syn_error(resp)
            resp.raise_for_status()
            off = 0
            for data in resp.iter_content(1024 * 1024):
                os.pwrite(self._fd, data, off)
                off += len(data)
                with self._counter_lock:
                    self.done_bytes = off
                if self.progress:
                    self.progress(off)
                if self.abort_check:
                    self.abort_check(off)
        with self._counter_lock:
            self.done_bytes = off


# ---------------------------------------------------------------- 进度显示
class ProgressBar:
    def __init__(self, total):
        self.total = total
        self.t0 = time.time()
        self.last_bytes = 0
        self.last_t = self.t0
        self.speed = 0.0

    def render(self, done, failures=0, force=False):
        now = time.time()
        if not force and now - self.last_t < 0.4:
            return
        dt = now - self.last_t
        if dt >= 1.0:
            self.speed = (done - self.last_bytes) / dt
            self.last_bytes, self.last_t = done, now
        pct = done * 100 / self.total if self.total else 0
        eta = (self.total - done) / self.speed if self.speed > 1 else 0
        bar_len = 24
        filled = int(bar_len * done / self.total) if self.total else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        tail = f" | 重试{failures}" if failures else ""
        sys.stdout.write(
            f"\r  {pct:5.1f}% |{bar}| {human(done)}/{human(self.total)}"
            f" | {human(self.speed)}/s | ETA {int(eta)}s{tail}   ")
        sys.stdout.flush()

    def finish(self):
        sys.stdout.write("\n")
        sys.stdout.flush()


# ---------------------------------------------------------------- 会话持久化
def save_session(client):
    SESSION_FILE.write_text(json.dumps(
        {"host": client.host, "qc_id": client.qc_id,
         "account": client.account, "sid": client.sid}))
    os.chmod(SESSION_FILE, 0o600)


def load_session(host):
    try:
        st = json.loads(SESSION_FILE.read_text())
    except Exception:
        return None
    if host:
        raw = host.strip().rstrip("/")
        norm = raw if "://" in raw else "https://" + raw
        # 既匹配解析后的完整地址,也匹配 QuickConnect ID 两种输入形式
        if st.get("host") != norm and st.get("qc_id") != raw.lower():
            return None
    return st


def clear_session():
    SESSION_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- 凭据存储
# 优先系统凭据库(macOS 钥匙串 / Windows 凭据管理器,通过 keyring 库);
# keyring 不可用时降级为本机混淆文件——只防"顺手翻看",不是加密,权限 600。
CRED_FILE = Path.home() / ".syn_dl_credentials.json"
CRED_SERVICE = "syn_dl"


def _xor_bytes(data, key):
    return bytes(a ^ b for a, b in zip(data, (key[i % len(key)] for i in range(len(data)))))


def _obfuscate(text, key):
    import base64
    return base64.b64encode(_xor_bytes(text.encode("utf-8"), key)).decode()


def _deobfuscate(b64text, key):
    import base64
    return _xor_bytes(base64.b64decode(b64text.encode()), key).decode("utf-8")


def _cred_file_read():
    try:
        return json.loads(CRED_FILE.read_text())
    except Exception:
        return {}


def _cred_file_write(data):
    CRED_FILE.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(CRED_FILE, 0o600)


def save_credentials(host_key, account, password, use_keyring=True):
    """保存登录凭据。返回 'keyring' 或 'file'。"""
    if use_keyring:
        try:
            import keyring
            keyring.set_password(CRED_SERVICE, host_key, password)
            data = _cred_file_read()
            data[host_key] = {"account": account, "pwd": None}   # 密码在钥匙串
            _cred_file_write(data)
            return "keyring"
        except Exception:
            pass
    key = hashlib.sha256(host_key.encode()).digest()
    data = _cred_file_read()
    data[host_key] = {"account": account, "pwd": _obfuscate(password, key)}
    _cred_file_write(data)
    return "file"


def load_credentials(host_key, use_keyring=True):
    """返回 (account, password);未保存过则 (None, None)。"""
    entry = _cred_file_read().get(host_key)
    if not entry:
        return (None, None)
    account = entry.get("account")
    if entry.get("pwd") is None:
        if use_keyring:
            try:
                import keyring
                pwd = keyring.get_password(CRED_SERVICE, host_key)
                if pwd:
                    return (account, pwd)
            except Exception:
                pass
        return (None, None)
    key = hashlib.sha256(host_key.encode()).digest()
    return (account, _deobfuscate(entry["pwd"], key))


def clear_credentials(host_key, use_keyring=True):
    data = _cred_file_read()
    if host_key in data:
        del data[host_key]
        _cred_file_write(data)
    if use_keyring:
        try:
            import keyring
            keyring.delete_password(CRED_SERVICE, host_key)
        except Exception:
            pass


def build_client(host, account=None, interactive=True):
    raw = (host or DEFAULT_HOST).strip()
    if not raw:
        # 没有指定地址:先用上次会话记住的,再退回交互询问
        last = load_session(None)
        if last:
            raw = last.get("qc_id") or last.get("host") or ""
        if not raw:
            if not interactive:
                raise SynError("未指定服务器地址(请用 --host 或先交互登录一次)")
            raw = input("服务器/QuickConnect ID: ").strip()
        if not raw:
            raise SynError("未输入有效的服务器地址")

    # 优先用缓存的中继地址验证会话,免去 QuickConnect 定位的往返
    cached = load_session(raw)
    if cached:
        client = SynClient(cached["host"])
        client.qc_id = cached.get("qc_id") or client.qc_id
        if client.try_sid(cached["sid"]):
            client.account = cached["account"]
            print(f"✔ 使用缓存会话登录:{client.account}")
            return client

    client = SynClient(raw)
    if client.qc_id:
        print(f"⇄ 正在通过 QuickConnect 定位 “{client.qc_id}”…")
        client.discover()
        print(f"⇄ 已定位到中继:{client.host}")
    else:
        client.discover()

    # 尝试用保存过的密码自动登录(忘记密码:交互内输入 logout)
    saved_acc, saved_pwd = load_credentials(client.qc_id or client.raw_host)
    if saved_pwd:
        acc = account or saved_acc
        try:
            client.login(acc, saved_pwd)
            save_session(client)
            print(f"✔ 使用已保存的密码自动登录:{acc}")
            return client
        except SynError as e:
            print(f"⚠ 已保存的密码登录失败({e}),请手动输入。")

    if not interactive:
        raise AuthExpired("缓存会话无效,请交互登录一次(不带 --get 直接运行)或检查账号")
    if cached:
        print("缓存会话已过期,请重新登录。")
    account = account or input("账号: ").strip()
    password = getpass.getpass("密码: ")
    try:
        client.login(account, password)
    except SynError as e:
        if "OTP" in str(e) or "验证码" in str(e):
            otp = input("一次性验证码(OTP): ").strip()
            client.login(account, password, otp)
        else:
            raise
    save_session(client)
    print(f"✔ 登录成功:{account}")
    return client


# ---------------------------------------------------------------- 下载编排
def download_file(client, remote_path, out=None, workers=8, interactive=True):
    remote_path = remote_path.strip().strip('"').strip("'")
    dest = Path(out) if out else Path.cwd() / remote_path.rsplit("/", 1)[-1]
    if dest.is_dir():
        dest = dest / remote_path.rsplit("/", 1)[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not dest.with_name(dest.name + ".synstate").exists():
        print(f"✋ 已存在 {dest}(如需重新下载请先删除它)")

    def request_fn(headers):
        return client.open_download(remote_path, headers)

    def reauth():
        client.ensure_sid(force=True)

    dl = RangeDownloader(request_fn, dest, workers=workers, reauth=reauth)
    print(f"⇣ {remote_path}")
    bar = None

    def progress(_done):
        nonlocal bar
        if bar is None and dl.size:
            bar = ProgressBar(dl.size)
            print(f"  → {dest}  ({human(dl.size)})")
        if bar:
            bar.render(dl.done_bytes, dl.failures)

    dl.progress = progress
    try:
        dl.run()
        if bar:
            bar.render(dl.done_bytes, force=True)
            bar.finish()
        print(f"✔ 下载完成:{dest}({human(dl.size)})")
        return dest
    except KeyboardInterrupt:
        if bar:
            bar.finish()
        print(f"\n⏸ 已中断,已下载 {human(dl.done_bytes)} / {human(max(dl.size, 1))}"
              f"({dl.done_bytes * 100 // max(dl.size, 1)}%)。"
              f"\n  进度已保存,重新运行同一命令即可续传。")
        raise SystemExit(130)
    except AuthExpired as e:
        if bar:
            bar.finish()
        print(f"\n✗ {e}\n  进度已保留({human(dl.done_bytes)} 已下载),"
              f"请重新运行程序登录后继续续传。")
        raise SystemExit(1)


# ---------------------------------------------------------------- 交互 shell
class Shell:
    def __init__(self, client):
        self.client = client
        self.cwd = "/"
        self._dir_cache = {}
        if "readline" in sys.modules:
            readline.set_completer(self._complete)
            readline.set_completer_delims(" \t\n")

    def _complete(self, text, state):
        matches = [n for n in self._names() if n.startswith(text)]
        return matches[state] if state < len(matches) else None

    def _names(self):
        try:
            key = self.cwd
            if key not in self._dir_cache:
                self._dir_cache[key] = self.client.list_dir(key)
            return [("/" if e["isdir"] else "") + e["name"]
                    for e in self._dir_cache[key]] or [e["name"] for e in self.client.list_shares()]
        except Exception:
            return []

    def _is_dir(self, name):
        return name.endswith("/")

    def run(self):
        print("\n命令:ls [路径] | cd 路径 | get 文件 | pwd | quit   (Tab 补全,路径含空格请加引号)")
        while True:
            try:
                line = input(f"{self.cwd}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            cmd, _, arg = line.partition(" ")
            arg = arg.strip()
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "pwd":
                    print(self.cwd)
                elif cmd == "logout":
                    cred_key = self.client.qc_id or self.client.raw_host
                    self.client.logout()
                    clear_session()
                    clear_credentials(cred_key)
                    print("已退出登录,并清除了缓存的会话与保存的密码。")
                    break
                elif cmd == "ls":
                    self.cmd_ls(arg)
                elif cmd == "cd":
                    self.cmd_cd(arg)
                elif cmd == "get":
                    if not arg:
                        print("用法:get <文件名>")
                        continue
                    self._dir_cache.pop(self.cwd, None)
                    download_file(self.client, self._resolve(arg))
                else:
                    print(f"未知命令:{cmd}")
            except AuthExpired:
                print("✗ 会话已过期。请重新登录(重启程序)。")
                break
            except SystemExit:
                pass
            except KeyboardInterrupt:
                print()
            except SynError as e:
                print(f"✗ {e}")

    def _resolve(self, p):
        p = p.strip().strip('"').strip("'")
        if not p.startswith("/"):
            p = (self.cwd.rstrip("/") + "/" + p) if self.cwd != "/" else "/" + p
        parts = []
        for seg in p.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        return "/" + "/".join(parts)

    def cmd_ls(self, arg):
        path = self._resolve(arg) if arg else self.cwd
        self._dir_cache.pop(path, None)
        entries = self.client.list_dir(path) if path != "/" else self.client.list_shares()
        if not entries:
            print("(空)")
        for e in entries:
            name = e["name"] + ("/" if e["isdir"] else "")
            size = human(e["size"]) if e.get("size") else ""
            print(f"  {'[目录]' if e['isdir'] else '[文件]'} {name:<50} {size:>12}")

    def cmd_cd(self, arg):
        if not arg:
            self.cwd = "/"
            return
        path = self._resolve(arg)
        entries = self.client.list_dir(path) if path != "/" else self.client.list_shares()
        if path != "/" and not entries:
            # 空目录也可能是合法目录;只有报错才到不了这里
            pass
        self.cwd = path
        self._dir_cache.pop(path, None)


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="群晖 DSM 下载客户端(多线程 + 断点续传)")
    ap.add_argument("--host",
                    help="DSM 地址或 QuickConnect ID(默认 %(default)s)")
    ap.add_argument("-u", "--user", help="账号")
    ap.add_argument("--ls", metavar="PATH", help="列出远程目录")
    ap.add_argument("--get", metavar="PATH", help="下载远程文件(支持断点续传)")
    ap.add_argument("-o", "--out", help="保存位置(目录或文件名)")
    ap.add_argument("-w", "--workers", type=int, default=8, help="并发线程数(默认 8)")
    args = ap.parse_args()

    client = build_client(args.host, args.user)

    if args.ls:
        shell_like = Shell(client)
        shell_like.cmd_ls(args.ls)
    elif args.get:
        download_file(client, args.get, out=args.out, workers=args.workers)
    else:
        Shell(client).run()


if __name__ == "__main__":
    main()
