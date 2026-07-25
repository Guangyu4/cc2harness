#!/usr/bin/env python3
"""端到端测试：内置一个假 SSH 服务器（paramiko ServerInterface），
通过 WebSocket 走完 认证 -> shell 回显 -> resize -> 关闭 的完整链路，
以及错误密码的失败路径。运行：python3 tests/e2e.py
"""

import json
import os
import socket
import sys
import threading

import paramiko
import tornado.testing
import tornado.websocket

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sshpro_app  # noqa: E402

FAKE_USER, FAKE_PASS = "demo", "demo-pass"


class FakeSSHServer(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        if (username, password) == (FAKE_USER, FAKE_PASS):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self._shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_exec_request(self, channel, command):
        # 监控采集的 exec 请求：直接返回空输出，验证前端优雅降级
        def run():
            channel.send_exit_status(0)
            channel.close()
        threading.Thread(target=run, daemon=True).start()
        return True

    @staticmethod
    def _shell(channel):
        try:
            channel.send(b"WELCOME-FAKE-SSHD\r\n")
            while True:
                data = channel.recv(4096)
                if not data:
                    break
                channel.send(b"echo:" + data)
        except OSError:
            pass
        finally:
            try:
                channel.close()
            except OSError:
                pass


def start_fake_sshd():
    """返回假 sshd 监听的端口。每个连接一个线程。"""
    host_key = paramiko.RSAKey.generate(2048)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]

    def accept_loop():
        while True:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            t = paramiko.Transport(client)
            t.add_server_key(host_key)
            try:
                t.start_server(server=FakeSSHServer())
            except (paramiko.SSHException, OSError, EOFError):
                pass

    threading.Thread(target=accept_loop, daemon=True).start()
    return port


class E2ETest(tornado.testing.AsyncHTTPTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sshd_port = start_fake_sshd()
        # 测试环境不用 tmux（避免在开发机上留下测试会话），直接用 bash
        sshpro_app.LOCAL_CMD = ["bash"]

    def get_app(self):
        return sshpro_app.make_app()

    def ws_url(self):
        return "ws://127.0.0.1:%d/ws" % self.get_http_port()

    @tornado.testing.gen_test(timeout=30)
    async def test_index_and_assets(self):
        http = self.get_url("/")
        client = tornado.httpclient.AsyncHTTPClient()
        for path in ("/", "/js/app.js", "/css/style.css",
                     "/vendor/xterm.js", "/vendor/xterm.css", "/vendor/addon-fit.js"):
            resp = await client.fetch(self.get_url(path))
            assert resp.code == 200, (path, resp.code)
            assert len(resp.body) > 100, path
        resp = await client.fetch(http)
        assert b"sshpro" in resp.body

    @tornado.testing.gen_test(timeout=30)
    async def test_wrong_password(self):
        ws = await tornado.websocket.websocket_connect(self.ws_url())
        ws.write_message(json.dumps({
            "type": "auth", "host": "127.0.0.1", "port": self.sshd_port,
            "username": FAKE_USER, "password": "wrong", "cols": 80, "rows": 24,
        }))
        msg = await ws.read_message()
        data = json.loads(msg)
        assert data["type"] == "error", data
        assert "认证失败" in data["message"], data
        assert await ws.read_message() is None  # 服务端应主动关闭

    @tornado.testing.gen_test(timeout=30)
    async def test_shell_roundtrip(self):
        ws = await tornado.websocket.websocket_connect(self.ws_url())
        ws.write_message(json.dumps({
            "type": "auth", "host": "127.0.0.1", "port": self.sshd_port,
            "username": FAKE_USER, "password": FAKE_PASS, "cols": 80, "rows": 24,
        }))

        got_ready = False
        term_bytes = b""
        ws_open = True
        for _ in range(50):
            msg = await ws.read_message()
            if msg is None:
                ws_open = False
                break
            if isinstance(msg, bytes):
                term_bytes += msg
            else:
                data = json.loads(msg)
                assert data["type"] != "error", data
                if data["type"] == "ready":
                    got_ready = True
                    ws.write_message(json.dumps({"type": "data", "data": "hello"}))
                    ws.write_message(json.dumps(
                        {"type": "resize", "cols": 120, "rows": 40}))
            if b"WELCOME-FAKE-SSHD" in term_bytes and b"echo:hello" in term_bytes:
                break

        assert got_ready, "never got ready"
        assert b"WELCOME-FAKE-SSHD" in term_bytes, term_bytes
        assert b"echo:hello" in term_bytes, term_bytes
        assert ws_open
        ws.close()

    @tornado.testing.gen_test(timeout=30)
    async def test_local_shell(self):
        ws = await tornado.websocket.websocket_connect(self.ws_url())
        ws.write_message(json.dumps(
            {"type": "auth", "local": True, "cols": 80, "rows": 24}))
        got_ready = False
        buf = b""
        for _ in range(80):
            msg = await ws.read_message()
            if msg is None:
                break
            if isinstance(msg, bytes):
                buf += msg
            else:
                data = json.loads(msg)
                assert data["type"] != "error", data
                if data["type"] == "ready":
                    got_ready = True
                    # 期望值用算式拼出来，避免命令回显本身干扰断言
                    ws.write_message(json.dumps(
                        {"type": "data", "data": "echo L0CAL-$((40+2))\n"}))
            if b"L0CAL-42" in buf:
                break
        assert got_ready, "never got ready"
        assert b"L0CAL-42" in buf, buf
        ws.close()


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
