#!/usr/bin/env python3
"""端到端测试（使用隔离的 tmux socket，不影响正在使用的会话）：
- 静态资源
- 会话打开 / 输入输出回显
- 断开重连后状态保留（自动恢复）
- 手动销毁会话
- 文件面板 API（列表 / 在线查看 / 下载）
运行：python3 tests/e2e.py
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

import tornado.httpclient
import tornado.testing
import tornado.websocket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sshpro_app  # noqa: E402

TEST_SOCKET = "sshprotest%d" % os.getpid()


class E2ETest(tornado.testing.AsyncHTTPTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert sshpro_app.TMUX, "测试需要 tmux"
        sshpro_app.TMUX_SOCKET = TEST_SOCKET

    @classmethod
    def tearDownClass(cls):
        subprocess.run([sshpro_app.TMUX, "-L", TEST_SOCKET, "kill-server"],
                       capture_output=True)
        super().tearDownClass()

    def get_app(self):
        return sshpro_app.make_app()

    def ws_url(self):
        return "ws://127.0.0.1:%d/ws" % self.get_http_port()

    async def _session(self, name):
        """打开一个会话并等到 ready + tmux attach 完成。"""
        ws = await tornado.websocket.websocket_connect(self.ws_url())
        ws.write_message(json.dumps(
            {"type": "auth", "session": name, "cols": 90, "rows": 26}))
        while True:
            msg = await ws.read_message()
            if isinstance(msg, bytes):
                continue
            data = json.loads(msg)
            assert data["type"] == "ready", data
            assert data["session"] == name
            break
        await asyncio.sleep(1.2)  # 等 tmux 完成 attach，再输入
        return ws

    async def _expect(self, ws, marker, max_msgs=300):
        buf = b""
        for _ in range(max_msgs):
            msg = await ws.read_message()
            if msg is None:
                break
            if isinstance(msg, bytes):
                buf += msg
                if marker in buf:
                    return buf
        raise AssertionError("%r not found; tail=%r" % (marker, buf[-400:]))

    @tornado.testing.gen_test(timeout=30)
    async def test_assets(self):
        client = tornado.httpclient.AsyncHTTPClient()
        for path in ("/", "/js/app.js", "/css/style.css",
                     "/vendor/xterm.js", "/vendor/xterm.css",
                     "/vendor/addon-fit.js"):
            resp = await client.fetch(self.get_url(path))
            assert resp.code == 200, (path, resp.code)
            assert len(resp.body) > 100, path
        resp = await client.fetch(self.get_url("/"))
        assert b"sshpro" in resp.body

    @tornado.testing.gen_test(timeout=40)
    async def test_recovery(self):
        """断开后重连同名会话，环境变量仍在 => 会话真正保活。"""
        ws = await self._session("sshpro-7")
        ws.write_message(json.dumps(
            {"type": "data", "data": "export MARK=$((4320+1)); echo GOT-$MARK\n"}))
        await self._expect(ws, b"GOT-4321")
        ws.close()
        await asyncio.sleep(0.5)

        ws2 = await self._session("sshpro-7")
        ws2.write_message(json.dumps({"type": "data", "data": "echo AGAIN-$MARK\n"}))
        await self._expect(ws2, b"AGAIN-4321")
        ws2.close()

    @tornado.testing.gen_test(timeout=40)
    async def test_sessions_list_and_destroy(self):
        ws = await self._session("sshpro-9")
        client = tornado.httpclient.AsyncHTTPClient()

        resp = await client.fetch(self.get_url("/api/sessions"))
        data = json.loads(resp.body)
        assert data["tmux"] is True
        assert "sshpro-9" in data["sessions"], data

        resp = await client.fetch(self.get_url("/api/kill?name=sshpro-9"),
                                  method="POST", body=b"")
        assert json.loads(resp.body)["ok"] is True

        # 会话被杀 -> PTY 退出 -> 服务端发 closed 并关闭 ws
        saw_closed = False
        for _ in range(100):
            msg = await ws.read_message()
            if msg is None:
                break
            if not isinstance(msg, bytes) and json.loads(msg)["type"] == "closed":
                saw_closed = True
        assert saw_closed

        resp = await client.fetch(self.get_url("/api/sessions"))
        assert "sshpro-9" not in json.loads(resp.body)["sessions"]

        # 非法会话名应被拒绝
        resp = await client.fetch(self.get_url("/api/kill?name=../evil"),
                                  method="POST", body=b"", raise_error=False)
        assert resp.code == 400

    @tornado.testing.gen_test(timeout=30)
    async def test_fs_api(self):
        tmpd = tempfile.mkdtemp(prefix="sshpro-fs-")
        content = "LINE-ONE\n中文内容测试\n"
        fpath = os.path.join(tmpd, "hello.log")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        os.mkdir(os.path.join(tmpd, "subdir"))
        client = tornado.httpclient.AsyncHTTPClient()
        q = urllib.parse.quote

        resp = await client.fetch(self.get_url("/api/fs/list?path=%s" % q(tmpd)))
        data = json.loads(resp.body)
        assert data["path"] == tmpd
        names = {e["name"]: e for e in data["entries"]}
        assert names["hello.log"]["size"] == len(content.encode())
        assert names["subdir"]["dir"] is True
        assert data["entries"][0]["name"] == "subdir"  # 目录排前面

        resp = await client.fetch(self.get_url("/api/fs/view?path=%s" % q(fpath)))
        assert "text/plain" in resp.headers["Content-Type"]
        assert "LINE-ONE" in resp.body.decode("utf-8")
        assert "中文内容测试" in resp.body.decode("utf-8")

        resp = await client.fetch(self.get_url("/api/fs/download?path=%s" % q(fpath)))
        assert resp.body.decode("utf-8") == content
        assert "attachment" in resp.headers["Content-Disposition"]

        resp = await client.fetch(
            self.get_url("/api/fs/view?path=%s" % q(tmpd + "/nope.txt")),
            raise_error=False)
        assert resp.code == 404


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
