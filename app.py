#!/usr/bin/env python3
"""sshpro — 基于 tmux 的网页终端。

浏览器 (xterm.js) <--WebSocket--> tornado <--PTY--> tmux 会话

每个标签页对应一个独立的 tmux 会话（sshpro / sshpro-2 / ...）：
断开连接、关闭浏览器只是「分离」，命令继续运行；重新打开页面自动恢复；
只有手动销毁（kill-session）才会真正结束会话。

WebSocket 协议（一个连接对应一个会话标签）：
  客户端 -> 服务端（JSON 文本帧）:
    {"type": "auth", "session": "sshpro-2", "cols": N, "rows": N}
    {"type": "data", "data": "..."}          # 键盘输入
    {"type": "resize", "cols": N, "rows": N}
  服务端 -> 客户端:
    二进制帧                                  # 终端原始输出（UTF-8 字节流）
    {"type": "ready", "session": "..."}      # PTY 已就绪
    {"type": "error", "message": "..."}
    {"type": "cwd", "path": "/..."}          # 终端当前目录变化（供文件面板跟随）
    {"type": "closed"}                       # 会话进程退出 / 被销毁

HTTP 接口：
    GET  /api/sessions                        # 列出可恢复的 tmux 会话
    POST /api/kill?name=sshpro-2              # 彻底销毁一个会话
    POST /api/upload                          # 粘贴图片上传，返回落盘路径
    GET  /api/fs/list?path=/a                 # 文件面板：目录列表
    GET  /api/fs/view?path=/a/b.log           # 在线查看（大文件取末尾）
    GET  /api/fs/download?path=/a/b           # 下载
"""

import argparse
import fcntl
import json
import logging
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import threading
import termios
import time
import urllib.parse
import uuid

import tornado.ioloop
import tornado.web
import tornado.websocket

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

CWD_INTERVAL = 1.5             # 终端状态（目录/鼠标模式）轮询间隔（秒）
VIEW_LIMIT = 2 * 1024 * 1024   # 在线查看最多返回字节数（大日志取末尾）

UPLOAD_DIR = os.path.expanduser("~/.sshpro/uploads")
UPLOAD_LIMIT = 20 * 1024 * 1024
UPLOAD_KEEP_DAYS = 7           # 上传的图片过期自动清理
IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/webp": "webp", "image/bmp": "bmp", "image/svg+xml": "svg",
           "image/tiff": "tiff", "image/avif": "avif"}

SESSION_PREFIX = "sshpro"
NAME_RE = re.compile(r"^sshpro[A-Za-z0-9_-]{0,32}$")

TMUX = shutil.which("tmux")
TMUX_SOCKET = None             # tmux -L 的 socket 名（测试隔离用）

SESSIONS = {}                  # sid -> TermHandler


def tmux_cmd(*args):
    cmd = [TMUX]
    if TMUX_SOCKET:
        cmd += ["-L", TMUX_SOCKET]
    cmd += list(args)
    return cmd


def tmux_run(*args, timeout=5):
    try:
        r = subprocess.run(tmux_cmd(*args), capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def list_tmux_sessions():
    """返回 [{"name": tmux 会话名, "label": 用户自定义名称}]。
    自定义名称存在 tmux 会话的 @label 用户选项里，随会话存亡。"""
    if not TMUX:
        return []
    code, out = tmux_run("list-sessions", "-F", "#{session_name}\t#{@label}")
    if code != 0:  # tmux server 未运行 => 无会话
        return []
    result = []
    for line in out.splitlines():
        name, _, label = line.partition("\t")
        if NAME_RE.match(name):
            result.append({"name": name, "label": label.strip()})
    result.sort(key=lambda s: s["name"])
    return result


def kill_session(name):
    """彻底销毁会话。tmux 模式杀 tmux 会话；退化模式给对应 shell 发 SIGHUP。"""
    if TMUX:
        code, _ = tmux_run("kill-session", "-t", "=" + name)
        return code == 0
    for h in list(SESSIONS.values()):
        if h.name == name and h.pty_pid:
            try:
                os.kill(h.pty_pid, signal.SIGHUP)
            except OSError:
                pass
            return True
    return False


def set_winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# ---------- 文件面板（本机文件系统） ----------

def fs_list(path):
    path = os.path.abspath(path or os.path.expanduser("~"))
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                is_dir = e.is_dir(follow_symlinks=True)
                size = 0 if is_dir else e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            entries.append({"name": e.name, "dir": is_dir, "size": size})
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": path, "entries": entries}


class BinaryFileError(Exception):
    pass


def fs_read_text(path, limit=VIEW_LIMIT):
    """读取文本内容；超过 limit 时取末尾（日志通常关心最新部分）。
    以内容嗅探判断文本/二进制（不看后缀），二进制抛 BinaryFileError。
    返回 (跳过的字节数, 文本)。"""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if b"\x00" in f.read(4096):
            raise BinaryFileError(path)
        skipped = 0
        if size > limit:
            skipped = size - limit
            f.seek(skipped)
        else:
            f.seek(0)
        data = f.read(limit)
    for enc in ("utf-8", "gbk"):
        try:
            return skipped, data.decode(enc)
        except UnicodeDecodeError:
            continue
    return skipped, data.decode("utf-8", "replace")


class TermHandler(tornado.websocket.WebSocketHandler):
    def open(self):
        self.loop = tornado.ioloop.IOLoop.current()
        self.name = None
        self.sid = None
        self.pty_fd = None
        self.pty_pid = None
        self.authed = False
        self.closed = False
        self.stop_evt = threading.Event()

    def on_message(self, message):
        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return
        mtype = msg.get("type")
        if not self.authed:
            if mtype == "auth":
                self.authed = True
                name = str(msg.get("session") or SESSION_PREFIX)
                if not NAME_RE.match(name):
                    self._send_json({"type": "error", "message": "非法的会话名"})
                    self.close()
                    return
                self._spawn(name, int(msg.get("cols") or 80),
                            int(msg.get("rows") or 24))
            return
        if mtype == "data" and self.pty_fd is not None:
            try:
                os.write(self.pty_fd, msg.get("data", "").encode())
            except OSError:
                pass
        elif mtype == "resize" and self.pty_fd is not None:
            try:
                set_winsize(self.pty_fd, int(msg["cols"]), int(msg["rows"]))
            except (OSError, KeyError, ValueError):
                pass

    def _spawn(self, name, cols, rows):
        if TMUX:
            # 附加后开启鼠标模式：滚轮直接滚动 tmux 历史（触控板平滑），
            # 否则滚轮会被转成方向键连发；调大历史行数；
            # set-clipboard + clipboard 终端能力：copy-mode 选中后
            # tmux 通过 OSC 52 把文本回传给浏览器写入剪贴板
            cmd = tmux_cmd(
                "new-session", "-A", "-s", name,
                ";", "set-option", "-g", "mouse", "on",
                ";", "set-option", "-g", "history-limit", "50000",
                ";", "set-option", "-s", "set-clipboard", "on",
                ";", "set-option", "-sa", "terminal-features",
                "xterm-256color:clipboard",
            )
        else:  # 无 tmux 的退化模式：普通 shell，无法断线恢复
            cmd = [os.environ.get("SHELL", "/bin/bash"), "-l"]
        try:
            pid, fd = pty.fork()
        except OSError as e:
            self._send_json({"type": "error", "message": "会话启动失败：%s" % e})
            self.close()
            return
        if pid == 0:  # 子进程：立即 exec，绝不返回
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ.setdefault("LANG", "C.UTF-8")
                os.chdir(os.path.expanduser("~"))
                os.execvp(cmd[0], cmd)
            except Exception:
                os._exit(127)
        self.name = name
        self.pty_pid = pid
        self.pty_fd = fd
        self.sid = uuid.uuid4().hex
        SESSIONS[self.sid] = self
        try:
            set_winsize(fd, cols, rows)
        except OSError:
            pass
        self._send_json({"type": "ready", "session": name, "tmux": bool(TMUX)})
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._cwd_loop, daemon=True).start()
        logging.info("session up: %s (%s)", name, " ".join(cmd))

    # ---- 以下方法运行在工作线程，回传均通过 loop.add_callback ----

    def _read_loop(self):
        fd = self.pty_fd
        try:
            while not self.closed:
                data = os.read(fd, 65536)
                if not data:
                    break
                self.loop.add_callback(self._send_bytes, data)
        except OSError:
            pass  # 子进程退出后 read 抛 EIO，属正常路径
        try:
            os.waitpid(self.pty_pid, 0)
        except (ChildProcessError, OSError):
            pass
        self.loop.add_callback(self._shell_closed)

    def _cwd_loop(self):
        """轮询终端状态（当前目录 + 前台程序是否接管鼠标），变化时推送前端。
        前端据 mouse 标志决定划选交给 tmux copy-mode 还是 xterm 本地选择。"""
        last = None
        while not self.stop_evt.wait(CWD_INTERVAL if last else 0.3):
            cur = self._pane_status()
            if cur[0] and cur != last:
                last = cur
                self.loop.add_callback(
                    self._send_json,
                    {"type": "status", "cwd": cur[0], "mouse": cur[1]})

    def _pane_status(self):
        """返回 (当前目录, 前台程序是否接管鼠标)；非 tmux 模式鼠标状态未知为 None。"""
        if TMUX and self.name:
            # 注意不能用 "=name" 精确匹配前缀：tmux 3.2a 的 display-message
            # 会静默返回空串；裸名称本身就是精确匹配优先
            code, out = tmux_run("display-message", "-p", "-t", self.name,
                                 "#{pane_current_path}\n#{mouse_any_flag}",
                                 timeout=3)
            lines = out.splitlines()
            if code == 0 and lines and lines[0].startswith("/"):
                mouse = len(lines) > 1 and lines[1].strip() == "1"
                return lines[0], mouse
        try:
            return os.readlink("/proc/%d/cwd" % self.pty_pid), None
        except OSError:
            return None, None

    # ---- 以下方法只在 IOLoop 线程调用 ----

    def _send_json(self, obj):
        try:
            self.write_message(json.dumps(obj))
        except tornado.websocket.WebSocketClosedError:
            pass

    def _send_bytes(self, data):
        try:
            self.write_message(data, binary=True)
        except tornado.websocket.WebSocketClosedError:
            pass

    def _shell_closed(self):
        if not self.closed:
            self._send_json({"type": "closed"})
            self.close()

    def on_close(self):
        self.closed = True
        self.stop_evt.set()
        SESSIONS.pop(self.sid, None)
        if self.pty_fd is not None:
            try:
                os.close(self.pty_fd)  # tmux 客户端收 SIGHUP 分离，会话继续存活
            except OSError:
                pass
            self.pty_fd = None


class StaticHandler(tornado.web.StaticFileHandler):
    """带 ?v= 的资源沿用 Tornado 的十年强缓存（换版本号即换 URL，安全）；
    但 index.html 没有版本号可换，Tornado 又不给它发 Cache-Control，
    浏览器就按启发式规则自行缓存 HTML，改了 ?v= 也送不到客户端。
    所以给它 no-cache：每次回源校验，没变照样走 304，几乎不费流量。"""

    def set_extra_headers(self, path):
        if "v" not in self.request.arguments:
            self.set_header("Cache-Control", "no-cache")


class SessionsHandler(tornado.web.RequestHandler):
    def get(self):
        self.write({"tmux": bool(TMUX), "sessions": list_tmux_sessions()})


class KillHandler(tornado.web.RequestHandler):
    def post(self):
        name = self.get_argument("name", "")
        if not NAME_RE.match(name):
            self.set_status(400)
            self.write({"error": "非法的会话名"})
            return
        ok = kill_session(name)
        self.write({"ok": ok})


class RenameHandler(tornado.web.RequestHandler):
    def post(self):
        name = self.get_argument("name", "")
        label = self.get_argument("label", "")
        if not NAME_RE.match(name):
            self.set_status(400)
            self.write({"error": "非法的会话名"})
            return
        label = "".join(c for c in label if c.isprintable()).strip()[:32]
        if not TMUX:
            self.write({"ok": False})
            return
        if label:
            code, _ = tmux_run("set-option", "-t", name, "@label", label)
        else:  # 空名称 = 恢复默认编号
            code, _ = tmux_run("set-option", "-u", "-t", name, "@label")
        self.write({"ok": code == 0})


def prune_uploads():
    cutoff = time.time() - UPLOAD_KEEP_DAYS * 86400
    try:
        with os.scandir(UPLOAD_DIR) as it:
            for e in it:
                try:
                    if e.is_file() and e.stat().st_mtime < cutoff:
                        os.unlink(e.path)
                except OSError:
                    continue
    except OSError:
        pass


class UploadHandler(tornado.web.RequestHandler):
    """浏览器里粘贴的图片落盘成文件，路径回给前端插进输入框——
    终端里的程序（Claude Code 等）没法收二进制，但认识路径。"""

    def post(self):
        ctype = (self.request.headers.get("Content-Type") or "") \
            .partition(";")[0].strip().lower()
        ext = IMG_EXT.get(ctype)
        if not ext:
            self.set_status(415)
            self.write({"error": "只支持图片（收到 %s）" % (ctype or "未知类型")})
            return
        body = self.request.body
        if not body:
            self.set_status(400)
            self.write({"error": "空文件"})
            return
        if len(body) > UPLOAD_LIMIT:
            self.set_status(413)
            self.write({"error": "图片超过 20MB"})
            return
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        name = "img-%s-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                 uuid.uuid4().hex[:4], ext)
        path = os.path.join(UPLOAD_DIR, name)
        with open(path, "wb") as f:
            f.write(body)
        prune_uploads()
        self.write({"path": path})


class FsHandler(tornado.web.RequestHandler):
    async def get(self, action):
        path = self.get_argument("path", "")
        loop = tornado.ioloop.IOLoop.current()
        try:
            if action == "list":
                self.write(await loop.run_in_executor(None, fs_list, path))
            elif action == "view":
                skipped, text = await loop.run_in_executor(None, fs_read_text, path)
                self.set_header("Content-Type", "text/plain; charset=utf-8")
                if skipped:
                    self.set_header("X-Sshpro-Skipped", str(skipped))
                self.write(text)
            else:
                await self._download(path, loop)
        except BinaryFileError:
            self.set_status(415)
            self.write({"error": "这是二进制文件，无法在线查看，请下载"})
        except FileNotFoundError:
            self.set_status(404)
            self.write({"error": "文件不存在"})
        except (PermissionError, IsADirectoryError) as e:
            self.set_status(403)
            self.write({"error": "无法访问：%s" % e.strerror})
        except OSError as e:
            self.set_status(500)
            self.write({"error": str(e)})

    async def _download(self, path, loop):
        f = await loop.run_in_executor(None, open, path, "rb")
        try:
            size = os.fstat(f.fileno()).st_size
            name = os.path.basename(path.rstrip("/")) or "download"
            self.set_header("Content-Type", "application/octet-stream")
            self.set_header("Content-Length", str(size))
            self.set_header("Content-Disposition",
                            "attachment; filename*=UTF-8''"
                            + urllib.parse.quote(name))
            while True:
                chunk = await loop.run_in_executor(None, f.read, 262144)
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()
        finally:
            f.close()


def make_app():
    return tornado.web.Application([
        (r"/ws", TermHandler),
        (r"/api/sessions", SessionsHandler),
        (r"/api/kill", KillHandler),
        (r"/api/rename", RenameHandler),
        (r"/api/upload", UploadHandler),
        (r"/api/fs/(list|view|download)", FsHandler),
        (r"/(.*)", StaticHandler,
         {"path": STATIC_DIR, "default_filename": "index.html"}),
    ])


def main():
    parser = argparse.ArgumentParser(description="sshpro — 基于 tmux 的网页终端")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认仅本机；页面无认证且可直接拿到 shell，"
                             "切勿裸露到不可信网络）")
    parser.add_argument("--port", type=int, default=8022, help="监听端口（默认 8022）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not TMUX:
        logging.warning("未找到 tmux：会话将无法断线恢复（建议安装 tmux）")
    app = make_app()
    app.listen(args.port, address=args.host)
    logging.info("sshpro running at http://%s:%d", args.host, args.port)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
