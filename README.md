# cc2harness — 把 Claude Code 搬到服务器上，用浏览器操控

## 为什么会有这个项目

受限于 Claude Code 对中国大陆的政策，在大陆的本机上几乎没有一种「安全」的运行方式：
无论用什么姿势在自己电脑上起 Claude Code，都要冒 Max Plan 账号被封的风险。

比较稳妥的做法是**让 Claude Code 只跑在合规地区的服务器上，本地只留一个浏览器**。
cc2harness 就是为这件事做的 harness：一个基于 tmux + xterm.js 的网页终端，
打开网页就是服务器上的 shell，在里面敲 `claude` 即可，本地机器上不装、不跑、不留任何东西。

```
本地浏览器 (xterm.js) ←WebSocket→ tornado ←PTY→ tmux 会话 → claude
       ↑ 仅通过 SSH 端口转发访问（这一步就是身份认证）
```

## 三个核心特性

- 📋 **复制是自由的**
  即使 Claude Code / vim / htop 这类程序接管了鼠标，也能直接划词，**选中即自动复制**
  （带 toast 提示）。有选中时 Cmd+C / Ctrl+C 是复制，没有选中时 Ctrl+C 照常发送中断。
  跨屏的大段输出可以边选边滚，不会像普通终端那样一滚就丢选区。

- 📂 **文件可以即时预览**
  右侧文件面板**自动跟随终端当前目录**（`cd` 到哪跟到哪）。查看是**按内容嗅探**的：
  `.md` / `.py` / 无后缀 / 日志……只要内容是文本就能直接在网页里打开
  （大文件自动取末尾，自动识别 UTF-8 / GBK），任意文件可下载。
  Claude 刚写完的文件，不用再 `cat` 一遍。

- ♻️ **会话是永久留存的**
  每个标签页是一个独立 tmux 会话。断网、关浏览器、合上电脑都只是「分离」，
  `claude` 继续在跑；重新打开页面自动恢复全部会话。
  **只有手动点 🗑 删除会话，连接才会真正结束。**

其余：多标签（＋ 新建会话）、双击标签重命名、完整交互式终端（256 色、UTF-8、
10000 行回滚、尺寸自适应）、平滑滚动、前端资源全部本地化（无 CDN 依赖）。

## 常见问题

<details>
<summary><b>为什么不用 FinalShell 这类终端？</b></summary>

因为对于运行在 Node.js 环境里的 Claude Code 来说，**这类工具里的输出并不是原生的**——
终端仿真是二手的，控制序列、重绘、颜色都要被它转译一道，Claude Code 的界面很容易花掉；
而且在 macOS 上的滚动体验也不好。

而对 web coding 来说，最重要的其实只有两件事：**把输出复制出来**，和**把输出看清楚**。
恰恰这两点是 shell 类工具做得都不太好的地方。cc2harness 直接用 VS Code 同款的 xterm.js
渲染原生字节流，并把「复制」和「看文件」做成了一等公民。

</details>

<details>
<summary><b>为什么不直接在本地运行 Claude Code？</b></summary>

因为本地会有**遥测、时区、甚至系统语言等多个检测点**同时运行，并不安全。

把它放到服务器上，本地就只剩一个浏览器标签页——不装 Node、不跑 CLI、不落任何配置，
检测面收敛到服务器一侧。

</details>

<details>
<summary><b>我要如何使用它？</b></summary>

把下面这段提示词整段复制给服务器上的另一个 Agent（任何能执行 shell 的 agent 都行），
让它替你完成部署：

````text
请在这台服务器上部署 cc2harness（一个基于 tmux 的网页终端，用来在浏览器里跑 Claude Code），
并在完成后把访问方式告诉我。要求如下：

1. 克隆并安装依赖：
   git clone https://github.com/Guangyu4/cc2harness.git ~/cc2harness
   pip3 install -r ~/cc2harness/requirements.txt        # 只需要 tornado
   确认 tmux 已安装（没有就 sudo apt install -y tmux，它是断线恢复的前提）。

2. 用 systemd 用户服务常驻，写入 ~/.config/systemd/user/cc2harness.service：
   [Unit]
   Description=cc2harness web terminal
   After=network.target

   [Service]
   WorkingDirectory=%h/cc2harness
   ExecStart=/usr/bin/python3 %h/cc2harness/app.py
   Restart=on-failure
   RestartSec=3
   # 重要：必须是 process。默认的 control-group 模式会在重启服务时
   # 把所有 tmux 会话一起杀掉，那样「会话永久留存」就没了
   KillMode=process

   [Install]
   WantedBy=default.target

   然后执行：
   systemctl --user daemon-reload
   systemctl --user enable --now cc2harness
   loginctl enable-linger $USER      # 开机自启、不登录也保持运行

3. 保持默认只监听 127.0.0.1:8022，不要改成 0.0.0.0，也不要配置任何公网入口——
   这个页面没有登录认证，打开就是 shell，只能通过 SSH 端口转发访问。

4. 验证：
   systemctl --user status cc2harness --no-pager
   curl -sI http://127.0.0.1:8022/ | head -1        # 期望 200
   python3 ~/cc2harness/tests/e2e.py                # 端到端测试，使用隔离 tmux socket

5. 确认服务器上已装好 Claude Code（node -v && claude --version），没有就装上。

6. 最后把我需要在本地执行的 SSH 端口转发命令原样给我（含真实用户名和主机名）。
````

部署完成后，在**本地电脑**上打开 Claude Code 的步骤：

```bash
# 1. 本地开一个端口转发（保持这个窗口开着，这一步同时就是身份认证）
ssh -L 8022:127.0.0.1:8022 user@server

# 2. 本地浏览器打开
http://127.0.0.1:8022

# 3. 在网页终端里进入项目目录，直接启动
cd ~/your-project
claude
```

之后就可以关掉浏览器、合上电脑、断网——`claude` 一直在服务器上跑。
下次再打开这个网址，标签页和会话原样恢复；只有点 🗑 才会真正结束它。

</details>

## 手动运行（不用 systemd 时）

```bash
pip3 install -r requirements.txt   # 仅需 tornado
sudo apt install tmux              # 没有 tmux 也能用，但断线不可恢复
python3 app.py                     # 默认监听 http://127.0.0.1:8022
```

```bash
nohup python3 app.py >/tmp/cc2harness.log 2>&1 &   # 临时后台
```

可选参数：`--host`（默认 `127.0.0.1`）、`--port`（默认 `8022`）。

## 测试

```bash
python3 tests/e2e.py
```

使用隔离的 tmux socket，覆盖：静态资源、输入输出回显、断线重连状态保留、
会话重命名与销毁、文件面板 API。

## 安全提示

- 默认只监听 `127.0.0.1`。页面**没有登录认证**，且打开就是本机 shell——
  谁能访问端口，谁就拥有运行用户的全部权限。请始终通过 SSH 端口转发访问；
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
