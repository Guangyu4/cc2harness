# sshpro — 网页版 SSH 终端

FinalShell 风格的 Web SSH 客户端：浏览器里输入服务器地址、用户名、密码即可连接，
终端由 [xterm.js](https://github.com/xtermjs/xterm.js)（VS Code 同款）渲染，
右侧带 FinalShell 标志性的实时系统监控面板。

```
浏览器 (xterm.js) ←WebSocket→ tornado ←paramiko→ 远程 SSH 服务器
```

## 功能

- ⚡ 打开页面**直接进入本机终端**（免密，默认挂在 tmux 里）：关掉浏览器、断网、
  合上电脑，正在跑的命令都不会中断，重新打开页面自动接回原会话
- 🖥️ 完整交互式终端：支持 vim / top / htop 等全屏程序、256 色、UTF-8 中文、10000 行回滚
- 📋 强制复制：即使 vim / htop / Claude Code 等程序接管了鼠标，也能直接划词选择，
  **选中即自动复制**；有选中内容时 Ctrl+C 为复制、无选中时照常发送中断；
  顶栏 ⧉ 按钮可开关（关闭后鼠标事件交还给远端程序）
- 📑 多标签页：本机会话 + 任意多个远程 SSH 会话，标签上有连接状态指示灯
- 📊 实时监控面板：CPU 使用率与负载、内存 / Swap、各磁盘分区用量、网络上下行速率、
  主机名 / 发行版 / 内核 / 运行时长（每 2.5 秒刷新；非 Linux 主机自动隐藏）
- 🕘 最近连接记录：自动记住主机 / 端口 / 用户名（**密码不落盘**），下次一键填充
- 📐 终端尺寸跟随浏览器窗口自适应

## 快速开始

```bash
pip3 install -r requirements.txt   # tornado + paramiko
python3 app.py                     # 默认监听 http://127.0.0.1:8022
```

浏览器打开 <http://127.0.0.1:8022>，直接进入本机终端；
点顶栏「＋」可再连接其他远程服务器（填主机 / 用户名 / 密码）。

常用参数：

```bash
python3 app.py --port 9000            # 换端口
python3 app.py --host 0.0.0.0         # 允许局域网访问（见下方安全提示）
python3 app.py --no-local             # 禁用免密本机会话（对外开放时必须加）
python3 app.py --local-cmd "bash -l"  # 本机会话不用 tmux（将失去断线保活能力）
```

本机会话默认运行 `tmux new -A -s sshpro`：断开连接只是「分离」，
重新打开页面自动「接回」，后台命令持续运行。想彻底结束会话时在终端里
输入 `exit`（或 `tmux kill-session -t sshpro`）即可。

后台常驻运行：

```bash
nohup python3 app.py >/tmp/sshpro.log 2>&1 &
```

## 测试

```bash
python3 tests/e2e.py
```

测试内置一个假 SSH 服务器，覆盖：静态资源、错误密码失败路径、
认证 → shell 回显 → resize 的完整链路。

## 安全提示

- 默认只监听 `127.0.0.1`。sshpro 页面本身**没有登录认证**，而且本机会话是免密的
  ——谁能打开页面谁就能拿到运行用户的 shell。请勿直接把 `0.0.0.0` 暴露到不可信网络；
  必须对外开放时务必加 `--no-local` 并配合下述 HTTPS + 认证。
- 在远程机器上部署时，推荐用 SSH 端口转发访问：
  `ssh -L 8022:127.0.0.1:8022 user@server`，然后本地浏览器打开 `http://127.0.0.1:8022`。
- 如需公网使用，请置于 Nginx 之后加 HTTPS + Basic Auth（WebSocket 需配置 `Upgrade` 头转发）。
- 远端主机密钥采用自动接受策略（与 FinalShell 等客户端默认行为一致）。

## 目录结构

```
app.py              # tornado 服务端：WebSocket ↔ paramiko 桥接 + 监控采集
static/index.html   # 页面骨架（登录卡片 / 标签栏 / 终端区 / 监控面板）
static/js/app.js    # 会话与标签管理、xterm 初始化、监控渲染
static/css/style.css
static/vendor/      # xterm.js 5.5.0 + fit addon（本地离线资源，无 CDN 依赖）
tests/e2e.py        # 端到端测试（内置假 sshd）
```
