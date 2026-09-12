#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QuickConnect 测试:ID 解析(307 重定向机制)、端到端下载、会话缓存两种输入形式匹配。"""
import hashlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import syn_dl  # noqa: E402
from syn_dl import (SynClient, download_file, load_session,  # noqa: E402
                    looks_like_qc_id, resolve_quickconnect, save_session)
from test_e2e import DATA, Handler, MD5, REMOTE_PATH, SIZE  # noqa: E402


class EntryHandler(BaseHTTPRequestHandler):
    """模拟 QuickConnect 区域入口:把 /webapi/* 307 到 NAS 实际地址。"""
    target = None

    def do_GET(self):
        if self.path.startswith("/webapi/"):
            self.send_response(307)
            self.send_header("Location", self.target + self.path)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    dsm = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    entry = ThreadingHTTPServer(("127.0.0.1", 0), EntryHandler)
    dsm_port, entry_port = dsm.server_address[1], entry.server_address[1]
    EntryHandler.target = f"http://127.0.0.1:{dsm_port}"
    for s in (dsm, entry):
        threading.Thread(target=s.serve_forever, daemon=True).start()

    # 1) ID 判定
    assert looks_like_qc_id("my-nas")
    assert looks_like_qc_id("My-Nas")
    assert not looks_like_qc_id("my-nas.us3.quickconnect.to")
    assert not looks_like_qc_id("192.168.1.10")
    assert not looks_like_qc_id("https://x.com")
    print("[qc] QuickConnect ID 判定 ✔")

    # 2) 解析:入口 307 → 最终 NAS 地址
    base = resolve_quickconnect("my-nas",
                                entry_base=f"http://127.0.0.1:{entry_port}")
    assert base == f"http://127.0.0.1:{dsm_port}", base
    print(f"[qc] ID 解析 ✔  入口 :{entry_port} → NAS :{dsm_port}")

    # 3) SynClient 用 ID 走完整流程:解析 → 登录 → 下载
    client = SynClient("my-nas")
    assert client.qc_id == "my-nas"
    client.discover(_entry_override=f"http://127.0.0.1:{entry_port}")
    assert client.host == f"http://127.0.0.1:{dsm_port}", client.host
    client.login("alice", "secret")
    shares = client.list_shares()
    assert shares[0]["path"] == "/media"
    out = Path(tempfile.mkdtemp()) / "out.mp3"
    download_file(client, REMOTE_PATH, out=out, workers=4)
    assert hashlib.md5(out.read_bytes()).hexdigest() == MD5
    print("[qc] 端到端(ID 登录 → 下载 md5 一致)✔")

    # 4) 会话缓存:ID 与解析后地址两种输入都要能命中
    tmp_session = Path(tempfile.mkdtemp()) / "session.json"
    orig = syn_dl.SESSION_FILE
    syn_dl.SESSION_FILE = tmp_session
    try:
        save_session(client)
        assert load_session("my-nas") is not None, "ID 形式应命中"
        assert load_session("My-Nas") is not None, "ID 大小写不敏感"
        assert load_session(f"http://127.0.0.1:{dsm_port}") is not None, "地址形式应命中"
        assert load_session("some-other-id") is None
        assert load_session("https://other.example.com") is None
    finally:
        syn_dl.SESSION_FILE = orig
    print("[qc] 会话缓存:ID/地址双形式匹配 ✔")

    dsm.shutdown()
    entry.shutdown()
    print("\n[qc] 全部通过 ✅")


if __name__ == "__main__":
    main()
