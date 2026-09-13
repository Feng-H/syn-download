#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试:模拟 DSM API(login / list / download),验证 SynClient 编码与下载编排。"""
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
from syn_dl import SynClient, download_file  # noqa: E402

SIZE = 3 * 1024 * 1024 + 777
DATA = os.urandom(SIZE)
MD5 = hashlib.md5(DATA).hexdigest()
GOOD_SID = "test-sid-12345"
REMOTE_PATH = "/media/Travel Notes/Trip Recording Day 01.mp3"


def _tobj(y, m, d):
    """真实 DSM 7 的 additional.time 形态:嵌套对象 + 格式化时间字符串。"""
    s = f"{y:04d}-{m:02d}-{d:02d} 12:00:00"
    return {"atime": s, "crtime": s, "ctime": s, "mtime": s}


def _ts(y, m, d):
    """指定日期正午(UTC)的 unix 时间戳(兼容分支:部分形态直接给数值)。"""
    import calendar
    return calendar.timegm((y, m, d, 12, 0, 0))


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        sid = q.get("_sid", [None])[0]

        if u.path == "/webapi/query.cgi":
            return self._json({"success": True, "data": {
                "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
                "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
                "SYNO.FileStation.Download": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
            }})

        api = q.get("api", [""])[0]
        if api == "SYNO.API.Auth":
            if q.get("account", [""])[0] == "alice" and q.get("passwd", [""])[0] == "secret":
                return self._json({"success": True, "data": {"sid": GOOD_SID}})
            return self._json({"success": False, "error": {"code": 400}})

        if sid != GOOD_SID:
            return self._json({"success": False, "error": {"code": 119}})

        if api == "SYNO.FileStation.List":
            if q.get("method", [""])[0] == "list_share":
                return self._json({"success": True, "data": {"shares": [
                    {"name": "media", "path": "/media"}]}})
            folder = json.loads(q.get("folder_path", ['""'])[0])
            return self._json({"success": True, "data": {"files": [
                {"name": "Travel Notes", "path": f"{folder}/Travel Notes", "isdir": True},
                {"name": "readme.txt", "path": f"{folder}/readme.txt", "isdir": False,
                 "additional": {"size": 5, "time": _tobj(2026, 8, 1)}},
                {"name": "Trip Recording Day 01.mp3",
                 "path": REMOTE_PATH, "isdir": False,
                 "additional": {"size": SIZE, "time": _tobj(2026, 9, 10)}},
                {"name": "Old Notes 2025.txt", "path": f"{folder}/Old Notes 2025.txt",
                 "isdir": False,
                 "additional": {"size": 9, "time": _ts(2025, 12, 31)}},
                {"name": "Future Plan.txt", "path": f"{folder}/Future Plan.txt",
                 "isdir": False, "additional": {"size": 3, "time": _tobj(2026, 12, 25)}},
                {"name": "No Time Stamp.bin", "path": f"{folder}/No Time Stamp.bin",
                 "isdir": False, "additional": {"size": 1}}]}})

        if api == "SYNO.FileStation.Download":
            path = json.loads(q.get("path", ['""'])[0])
            assert path == REMOTE_PATH, f"路径解码不符: {path!r}"
            rng = self.headers.get("Range", "")
            m = re.match(r"bytes=(\d+)-(\d*)$", rng)
            if m:
                s, e = int(m.group(1)), int(m.group(2) or SIZE - 1)
                e = min(e, SIZE - 1)
                body = DATA[s:e + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {s}-{e}/{SIZE}")
            else:
                body = DATA
                self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        return self._json({"success": False, "error": {"code": 101}})

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 登录(错误密码 → 报错;正确密码 → 成功)
    c = SynClient(f"http://127.0.0.1:{port}")
    c.discover()
    try:
        c.login("alice", "wrong")
        raise AssertionError("错误密码应当登录失败")
    except Exception:
        pass
    c.login("alice", "secret")
    assert c.sid == GOOD_SID
    print("[e2e] 登录(含错误密码拒绝)✔")

    shares = c.list_shares()
    assert shares[0]["path"] == "/media"
    files = c.list_dir("/media")
    assert any(f["name"] == "Travel Notes" and f["isdir"] for f in files)
    mtimes = {f["name"]: f["mtime"] for f in files}
    assert mtimes["Trip Recording Day 01.mp3"] == "2026-09-10", \
        f"嵌套对象格式未归一化: {mtimes['Trip Recording Day 01.mp3']!r}"
    assert mtimes["Old Notes 2025.txt"] == "2025-12-31", \
        f"unix 数值格式未归一化: {mtimes['Old Notes 2025.txt']!r}"
    assert mtimes["No Time Stamp.bin"] is None, "无时间字段应为 None"
    print("[e2e] mtime 归一化(对象/数值/缺失)✔")
    print("[e2e] list_share / list(含空格/括号路径)✔")

    out = Path(tempfile.mkdtemp()) / "out.mp3"
    download_file(c, f'"{REMOTE_PATH}"', out=out, workers=4)
    assert hashlib.md5(out.read_bytes()).hexdigest() == MD5, "md5 不匹配"
    print(f"[e2e] 下载完成 md5 一致 ✔  ({out})")

    srv.shutdown()
    print("\n[e2e] 全部通过 ✅")


if __name__ == "__main__":
    main()
