#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试:验证 RangeDownloader 的多线程、断点续传、认证过期恢复。"""
import hashlib
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from syn_dl import RangeDownloader, AuthExpired  # noqa: E402

import requests  # noqa: E402

SIZE = 8 * 1024 * 1024 + 12345   # 非整块大小,覆盖边界
DATA = os.urandom(SIZE)
MD5 = hashlib.md5(DATA).hexdigest()
CHUNK = 1024 * 1024              # 1MB 块,便于多块并发

state = {"auth_failures_left": 2}  # 前两次请求模拟会话过期
state_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with state_lock:
            fail = state["auth_failures_left"] > 0
            if fail:
                state["auth_failures_left"] -= 1
            break_start = state.get("break_once", set())
        if fail:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = b'{"error":{"code":119},"success":false}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d*)$", rng)
        if m:
            s = int(m.group(1))
            e = int(m.group(2)) if m.group(2) else SIZE - 1
            e = min(e, SIZE - 1)
            # 模拟传输中断:首次命中指定块时只发一半数据就断开
            if break_start and s in break_start:
                with state_lock:
                    state["break_once"].discard(s)
                half = DATA[s:s + (e - s + 1) // 2]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {s}-{e}/{SIZE}")
                self.send_header("Content-Length", str(e - s + 1))  # 声明完整长度
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(half)
                self.close_connection = True
                return
            body = DATA[s:e + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {s}-{e}/{SIZE}")
        else:
            body = DATA
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def make_request_fn(url):
    count = {"n": 0}

    def fn(headers):
        count["n"] += 1
        return requests.get(url, headers=headers, stream=True, timeout=10)

    fn.count = count
    return fn


def reauth():
    with state_lock:
        state["auth_failures_left"] = 0  # 模拟重新登录成功
    reauth.calls += 1


reauth.calls = 0


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/file.bin"
    print(f"[test] server on {url}, file {SIZE} bytes, md5 {MD5}")

    tmp = Path(tempfile.mkdtemp())
    dest = tmp / "file.bin"

    # ---- 第 1 轮:下载到 ~40% 强制中止 ----
    abort_at = SIZE * 40 // 100

    def abort_hook(done):
        if done >= abort_at:
            raise KeyboardInterrupt

    dl1 = RangeDownloader(make_request_fn(url), dest, workers=4, chunk=CHUNK,
                          reauth=reauth, abort_check=abort_hook)
    try:
        dl1.run()
        raise AssertionError("第一轮应当被中止")
    except KeyboardInterrupt:
        pass
    got = dest.stat().st_size
    done_pct = dl1.done_bytes * 100 // SIZE
    assert got == SIZE, f"预分配大小错误 {got}"
    assert dest.with_name(dest.name + ".synstate").exists(), "缺少状态文件"
    assert 30 <= done_pct <= 60, f"中止时进度异常 {done_pct}%"
    print(f"[test] 第 1 轮中止于 {done_pct}%({dl1.done_bytes} B),状态文件存在 ✔")

    # ---- 第 2 轮:续传到完成 ----
    dl2 = RangeDownloader(make_request_fn(url), dest, workers=4, chunk=CHUNK,
                          reauth=reauth)
    n = dl2.run()
    assert n == SIZE
    md5 = hashlib.md5(dest.read_bytes()).hexdigest()
    assert md5 == MD5, "md5 不匹配"
    state_f = dest.with_name(dest.name + ".synstate")
    assert not state_f.exists(), "完成后状态文件应删除"
    time.sleep(3.5)  # 覆盖 saver 线程的保存周期,确认状态文件不会被复活
    assert not state_f.exists(), "完成后状态文件被 saver 线程复活了(竞态回归)"
    print(f"[test] 第 2 轮续传完成,md5 一致 ✔,状态文件已清理且未复活 ✔")

    # ---- 第 3 轮:认证过期自动重登 ----
    dest3 = tmp / "file3.bin"
    with state_lock:
        state["auth_failures_left"] = 2
    dl3 = RangeDownloader(make_request_fn(url), dest3, workers=4, chunk=CHUNK,
                          reauth=reauth)
    dl3.run()
    md53 = hashlib.md5(dest3.read_bytes()).hexdigest()
    assert md53 == MD5, "第 3 轮 md5 不匹配"
    assert reauth.calls >= 1, "应当触发过重登"
    print(f"[test] 会话过期 → 自动重登 → 重试成功 ✔ (reauth 调用 {reauth.calls} 次)")

    # ---- 第 4 轮:分块中途断开重试,进度不得重复计数(>100% 回归) ----
    dest4 = tmp / "file4.bin"
    with state_lock:
        state["auth_failures_left"] = 0
        state["break_once"] = {CHUNK * 2, CHUNK * 5}   # 第 2、6 块首传必断
    max_seen = [0]

    def track(done):
        max_seen[0] = max(max_seen[0], done)

    dl4 = RangeDownloader(make_request_fn(url), dest4, workers=4, chunk=CHUNK,
                          progress=track)
    dl4.run()
    md54 = hashlib.md5(dest4.read_bytes()).hexdigest()
    assert md54 == MD5, "第 4 轮 md5 不匹配"
    assert max_seen[0] <= SIZE, f"进度超过 100%:{max_seen[0]} > {SIZE}"
    assert dl4.done_bytes == SIZE
    print(f"[test] 分块中途断开 → 重试 → 进度峰值 {max_seen[0]}/{SIZE},未超 100% ✔")

    srv.shutdown()
    print("\n[test] 全部通过 ✅")


if __name__ == "__main__":
    main()
