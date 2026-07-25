#!/usr/bin/env python3
"""sshpro — FinalShell 风格的网页版 SSH 终端。

浏览器 (xterm.js) <--WebSocket--> tornado <--paramiko--> 远程 SSH 服务器

协议（一个 WebSocket 对应一次 SSH 会话）：
  客户端 -> 服务端（JSON 文本帧）:
    {"type": "auth", "host", "port", "username", "password", "cols", "rows"}
    {"type": "data", "data": "..."}          # 键盘输入
    {"type": "resize", "cols": N, "rows": N}
  服务端 -> 客户端:
    二进制帧                                  # 终端原始输出（UTF-8 字节流，交给 xterm 解码）
    {"type": "ready"}                        # 认证成功，shell 已就绪
    {"type": "error", "message": "..."}      # 连接/认证失败
    {"type": "sysinfo", ...}                 # 主机静态信息（hostname / 内核 / 核数 / 发行版）
    {"type": "stats", ...}                   # 周期性资源占用（CPU / 内存 / 磁盘 / 网络）
    {"type": "closed"}                       # 远端 shell 已退出
"""

import argparse
import fcntl
import json
import logging
import os
import pty
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import time
import termios

import paramiko
import tornado.ioloop
import tornado.web
import tornado.websocket

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

STATS_INTERVAL = 2.5  # 秒

INFO_CMD = (
    "hostname; uname -r; nproc; "
    "(. /etc/os-release 2>/dev/null && echo \"$PRETTY_NAME\") || uname -s"
)

STATS_CMD = (
    "head -1 /proc/stat; echo @@@; "
    "cat /proc/meminfo; echo @@@; "
    "df -PB1; echo @@@; "
    "cat /proc/loadavg; echo @@@; "
    "cat /proc/net/dev; echo @@@; "
    "cat /proc/uptime"
)


def default_local_cmd():
    """本机会话的启动命令。优先 tmux：断开连接只是分离（detach），
    正在运行的命令继续执行，重新打开页面时 -A 自动接回同一会话。"""
    if shutil.which("tmux"):
        return ["tmux", "new-session", "-A", "-s", "sshpro"]
    return [os.environ.get("SHELL", "/bin/bash"), "-l"]


LOCAL_CMD = default_local_cmd()
LOCAL_ENABLED = True


def set_winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def parse_stats(raw):
    """把 STATS_CMD 的输出解析成结构化数据；任何一段解析失败都跳过该段。"""
    parts = raw.split("@@@")
    out = {}
    try:  # /proc/stat 第一行: cpu user nice system idle iowait irq softirq steal ...
        fields = parts[0].split()
        if fields and fields[0] == "cpu":
            out["cpu_ticks"] = [int(x) for x in fields[1:]]
    except (IndexError, ValueError):
        pass
    try:  # /proc/meminfo（单位 kB）
        mem = {}
        for line in parts[1].splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                mem[key.strip()] = int(val.split()[0]) * 1024
        out["mem"] = {
            "total": mem.get("MemTotal", 0),
            "avail": mem.get("MemAvailable", mem.get("MemFree", 0)),
            "swap_total": mem.get("SwapTotal", 0),
            "swap_free": mem.get("SwapFree", 0),
        }
    except (IndexError, ValueError):
        pass
    try:  # df -PB1
        disks, seen = [], set()
        for line in parts[2].splitlines()[1:]:
            f = line.split()
            if len(f) >= 6 and f[0].startswith("/dev/") and "loop" not in f[0]:
                if f[5] not in seen:
                    seen.add(f[5])
                    disks.append({"mount": f[5], "total": int(f[1]), "used": int(f[2])})
        disks.sort(key=lambda d: d["total"], reverse=True)
        out["disks"] = disks[:4]
    except (IndexError, ValueError):
        pass
    try:
        out["load"] = [float(x) for x in parts[3].split()[:3]]
    except (IndexError, ValueError):
        pass
    try:  # /proc/net/dev：除 lo 外所有网卡收发字节合计
        rx = tx = 0
        for line in parts[4].splitlines():
            if ":" in line:
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                f = data.split()
                rx += int(f[0])
                tx += int(f[8])
        out["net"] = {"rx": rx, "tx": tx}
    except (IndexError, ValueError):
        pass
    try:
        out["uptime"] = float(parts[5].split()[0])
    except (IndexError, ValueError):
        pass
    return out


class TermHandler(tornado.websocket.WebSocketHandler):
    def open(self):
        self.loop = tornado.ioloop.IOLoop.current()
        self.ssh = None
        self.chan = None
        self.mode = None          # "ssh" | "local"
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
                self.authed = True  # 防止重复 auth
                if msg.get("local"):
                    if LOCAL_ENABLED:
                        self._connect_local(msg)
                    else:
                        self._send_json({"type": "error",
                                         "message": "本机会话已被禁用（--no-local）"})
                        self.close()
                else:
                    threading.Thread(target=self._connect, args=(msg,),
                                     daemon=True).start()
            return
        if mtype == "data":
            try:
                if self.pty_fd is not None:
                    os.write(self.pty_fd, msg.get("data", "").encode())
                elif self.chan:
                    self.chan.send(msg.get("data", "").encode())
            except OSError:
                pass
        elif mtype == "resize":
            try:
                cols, rows = int(msg["cols"]), int(msg["rows"])
                if self.pty_fd is not None:
                    set_winsize(self.pty_fd, cols, rows)
                elif self.chan:
                    self.chan.resize_pty(width=cols, height=rows)
            except (OSError, KeyError, ValueError):
                pass

    def _connect_local(self, msg):
        """本机会话：直接 fork 一个 PTY 运行 LOCAL_CMD（默认 tmux），无需密码。
        在 IOLoop 线程内调用。"""
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)
        try:
            pid, fd = pty.fork()
        except OSError as e:
            self._send_json({"type": "error", "message": "本机会话启动失败：%s" % e})
            self.close()
            return
        if pid == 0:  # 子进程：立即 exec，绝不返回
            try:
                os.environ["TERM"] = "xterm-256color"
                os.environ.setdefault("LANG", "C.UTF-8")
                os.chdir(os.path.expanduser("~"))
                os.execvp(LOCAL_CMD[0], LOCAL_CMD)
            except Exception:
                os._exit(127)
        self.mode = "local"
        self.pty_pid = pid
        self.pty_fd = fd
        try:
            set_winsize(fd, cols, rows)
        except OSError:
            pass
        self._send_json({"type": "ready"})
        threading.Thread(target=self._read_loop_local, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        logging.info("local session up: %s", " ".join(LOCAL_CMD))

    # ---- 以下方法运行在工作线程，回传均通过 loop.add_callback ----

    def _read_loop_local(self):
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

    def _connect(self, msg):
        host = str(msg.get("host", "")).strip()
        port = int(msg.get("port") or 22)
        username = str(msg.get("username", "")).strip()
        password = str(msg.get("password", ""))
        cols = int(msg.get("cols") or 80)
        rows = int(msg.get("rows") or 24)
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                host, port=port, username=username, password=password,
                timeout=10, auth_timeout=15, banner_timeout=15,
                allow_agent=False, look_for_keys=False,
            )
            chan = ssh.invoke_shell(term="xterm-256color", width=cols, height=rows)
        except paramiko.AuthenticationException:
            self._fail("认证失败：用户名或密码错误")
            return
        except (paramiko.SSHException, socket.error, OSError) as e:
            self._fail("连接失败：%s" % e)
            return
        self.mode = "ssh"
        self.ssh = ssh
        self.chan = chan
        self.loop.add_callback(self._send_json, {"type": "ready"})
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        logging.info("session up: %s@%s:%s", username, host, port)

    def _fail(self, message):
        logging.warning("connect failed: %s", message)
        def send_and_close():
            self._send_json({"type": "error", "message": message})
            self.close()
        self.loop.add_callback(send_and_close)

    def _read_loop(self):
        """远端 shell 输出 -> 浏览器（原始字节，二进制帧）。"""
        try:
            while not self.closed:
                data = self.chan.recv(65536)
                if not data:
                    break
                self.loop.add_callback(self._send_bytes, data)
        except OSError:
            pass
        self.loop.add_callback(self._shell_closed)

    def _monitor_loop(self):
        """周期采集远端资源占用；非 Linux 主机会解析失败并自动静默。"""
        info = self._exec(INFO_CMD)
        if info is not None:
            lines = info.splitlines()
            if len(lines) >= 4:
                self.loop.add_callback(self._send_json, {
                    "type": "sysinfo",
                    "hostname": lines[0].strip(),
                    "kernel": lines[1].strip(),
                    "cores": lines[2].strip(),
                    "os": lines[3].strip(),
                })
        prev = None
        prev_t = None
        while not self.stop_evt.wait(STATS_INTERVAL if prev else 0.2):
            raw = self._exec(STATS_CMD)
            if raw is None:
                continue
            now = time.monotonic()
            cur = parse_stats(raw)
            if not cur:
                return  # 远端没有 /proc，放弃监控
            stats = {"type": "stats"}
            if "cpu_ticks" in cur and prev and "cpu_ticks" in prev:
                d = [a - b for a, b in zip(cur["cpu_ticks"], prev["cpu_ticks"])]
                total = sum(d)
                if total > 0:
                    idle = d[3] + (d[4] if len(d) > 4 else 0)  # idle + iowait
                    stats["cpu"] = round(100.0 * (total - idle) / total, 1)
            if "mem" in cur:
                m = cur["mem"]
                stats["mem"] = {"total": m["total"], "used": m["total"] - m["avail"]}
                stats["swap"] = {"total": m["swap_total"],
                                 "used": m["swap_total"] - m["swap_free"]}
            if "disks" in cur:
                stats["disks"] = cur["disks"]
            if "load" in cur:
                stats["load"] = cur["load"]
            if "net" in cur and prev and "net" in prev and prev_t:
                dt = max(now - prev_t, 0.001)
                stats["net"] = {
                    "rx": max(cur["net"]["rx"] - prev["net"]["rx"], 0) / dt,
                    "tx": max(cur["net"]["tx"] - prev["net"]["tx"], 0) / dt,
                }
            if "uptime" in cur:
                stats["uptime"] = cur["uptime"]
            prev, prev_t = cur, now
            if len(stats) > 1:
                self.loop.add_callback(self._send_json, stats)

    def _exec(self, cmd, timeout=8):
        if self.mode == "local":
            try:
                r = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                   timeout=timeout)
                return r.stdout.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                return None
        try:
            chan = self.ssh.get_transport().open_session(timeout=timeout)
            chan.settimeout(timeout)
            chan.exec_command(cmd)
            buf = b""
            while True:
                d = chan.recv(65536)
                if not d:
                    break
                buf += d
            chan.close()
            return buf.decode("utf-8", "replace")
        except (paramiko.SSHException, socket.error, OSError, AttributeError):
            return None

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
        if self.pty_fd is not None:
            try:
                os.close(self.pty_fd)  # tmux 客户端收到 SIGHUP 分离，会话继续存活
            except OSError:
                pass
            self.pty_fd = None
        for res in (self.chan, self.ssh):
            if res is not None:
                try:
                    res.close()
                except OSError:
                    pass


def make_app():
    return tornado.web.Application([
        (r"/ws", TermHandler),
        (r"/(.*)", tornado.web.StaticFileHandler,
         {"path": STATIC_DIR, "default_filename": "index.html"}),
    ])


def main():
    parser = argparse.ArgumentParser(description="sshpro — web SSH terminal")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认仅本机；局域网访问用 0.0.0.0，注意加防护）")
    parser.add_argument("--port", type=int, default=8022, help="监听端口（默认 8022）")
    parser.add_argument("--no-local", action="store_true",
                        help="禁用免密的本机会话（例如监听 0.0.0.0 时务必开启）")
    parser.add_argument("--local-cmd", default=None,
                        help='本机会话启动命令（默认自动选择 tmux，如 --local-cmd "bash -l"）')
    args = parser.parse_args()

    global LOCAL_ENABLED, LOCAL_CMD
    if args.no_local:
        LOCAL_ENABLED = False
    if args.local_cmd:
        LOCAL_CMD = shlex.split(args.local_cmd)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = make_app()
    app.listen(args.port, address=args.host)
    logging.info("sshpro running at http://%s:%d", args.host, args.port)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
