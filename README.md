# sshpro — 基于 tmux 的网页终端

在浏览器里直接使用服务器终端：打开页面即进入本机 shell，无需输入任何账号密码
（访问通过 SSH 端口转发完成认证）。终端由 [xterm.js](https://github.com/xtermjs/xterm.js)
（VS Code 同款）渲染，每个标签页是一个独立的 tmux 会话——**断网、关浏览器、
合上电脑都不会中断正在运行的命令**，重新打开页面自动恢复所有会话。

```
浏览器 (xterm.js) ←WebSocket→ tornado ←PTY→ tmux 会话（sshpro / sshpro-2 / ...）
```

## 功能

- ⚡ **打开即用**：页面加载后自动恢复全部 tmux 会话，各占一个标签页
- ♻️ **永不丢会话**：断开只是「分离」，命令继续跑；点击断开的标签或刷新页面即恢复；
  只有点 🗑 手动销毁才会真正结束会话
- ➕ **多标签**：「＋」新建终端 = 新建一个独立 tmux 会话
- 📋 **强制复制**：即使 vim / htop / Claude Code 等程序接管了鼠标也能直接划词，
  **选中即自动复制**（有 toast 提示）；有选中时 Cmd+C / Ctrl+C 为复制，
  无选中时 Ctrl+C 照常发送中断；顶栏 ⧉ 可开关
- 📂 **文件面板**：右侧目录列表**自动跟随终端当前目录**（`cd` 到哪跟到哪）；
  任意文件可下载；txt / log / out 文件可在线查看（大日志自动取末尾，
  自动识别 UTF-8 / GBK 编码）；手动进入其他目录后点 ⇄ 恢复跟随
- 🖥️ 完整交互式终端：vim / htop 等全屏程序、256 色、UTF-8 中文、10000 行回滚，
  尺寸随窗口自适应

## 快速开始

```bash
pip3 install -r requirements.txt   # 仅需 tornado
sudo apt install tmux              # 若未安装（没有 tmux 也能用，但断线不可恢复）
python3 app.py                     # 默认监听 http://127.0.0.1:8022
```

推荐从本地电脑通过 SSH 端口转发访问（这一步就是身份认证）：

```bash
ssh -L 8022:127.0.0.1:8022 user@server
# 然后本地浏览器打开 http://127.0.0.1:8022
```

后台常驻（systemd 用户服务示例见下）或临时：

```bash
nohup python3 app.py >/tmp/sshpro.log 2>&1 &
```

## systemd 常驻服务（推荐）

`~/.config/systemd/user/sshpro.service`：

```ini
[Unit]
Description=sshpro web terminal
After=network.target

[Service]
WorkingDirectory=%h/sshpro
ExecStart=/usr/bin/python3 %h/sshpro/app.py
Restart=on-failure
RestartSec=3
# 重要：只杀主进程。tmux server 在服务内启动，
# 默认的 control-group 模式会在重启服务时杀掉所有 tmux 会话
KillMode=process

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now sshpro
loginctl enable-linger $USER    # 开机自启、无需登录
```

## 测试

```bash
python3 tests/e2e.py
```

使用隔离的 tmux socket，覆盖：静态资源、输入输出回显、断线重连状态保留、
会话销毁、文件面板 API。

## 安全提示

- 默认只监听 `127.0.0.1`。页面**没有登录认证**，且打开就是本机 shell——
  谁能访问端口谁就拥有运行用户的全部权限。请始终通过 SSH 端口转发访问；
  **绝不要**把它直接暴露到公网或不可信局域网。
- 如确需远程直连，请置于 Nginx 之后加 HTTPS + 认证（WebSocket 需转发 `Upgrade` 头）。

## 目录结构

```
app.py              # tornado 服务端：WebSocket↔PTY(tmux) 桥接、会话/文件 API
static/index.html   # 页面骨架（标签栏 / 终端区 / 文件面板 / 查看器）
static/js/app.js    # 会话与标签管理、xterm 初始化、强制复制、文件面板
static/css/style.css
static/vendor/      # xterm.js 5.5.0 + fit addon（本地离线资源，无 CDN 依赖）
tests/e2e.py        # 端到端测试（隔离 tmux socket）
```
