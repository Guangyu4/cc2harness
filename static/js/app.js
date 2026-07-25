/* sshpro 前端：tmux 多会话标签页 + xterm.js 终端 + 目录跟随文件面板 */
"use strict";

const $ = (sel) => document.querySelector(sel);

const TERM_THEME = {
  background: "#0a0e14",
  foreground: "#c9d4e3",
  cursor: "#38bdf8",
  cursorAccent: "#0a0e14",
  selectionBackground: "#264f78",
  black: "#1c2431", red: "#f07178", green: "#7fd88f", yellow: "#e6c07b",
  blue: "#6cb2f7", magenta: "#c792ea", cyan: "#56b6c2", white: "#d7dde8",
  brightBlack: "#5c6b80", brightRed: "#ff8b92", brightGreen: "#95e6a1",
  brightYellow: "#f2cf87", brightBlue: "#82c4ff", brightMagenta: "#d9a9f5",
  brightCyan: "#6bd8e4", brightWhite: "#eaf0f8",
};

/* ---------- 小工具 ---------- */

function fmtBytes(n) {
  if (!isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + units[i];
}

let toastTimer = null;
function toast(msg, bad) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("bad", !!bad);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

/* ---------- 剪贴板 ---------- */

function copyFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  if (active) active.term.focus();
  if (ok) toast(`已复制 ${text.length} 个字符`);
  else toast("复制失败：浏览器拦截了剪贴板写入", true);
}

function copyText(text) {
  if (!text) return;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text)
      .then(() => toast(`已复制 ${text.length} 个字符`))
      .catch(() => copyFallback(text));
  } else {
    copyFallback(text);
  }
}

/* 强制选择模式：即使远端程序（vim/htop/claude 等）接管了鼠标，
   划词依然走本地选择——给鼠标事件强行加上修饰键（macOS 为 Option，
   其余平台为 Shift），等效于按住修饰键拖选，但无需真的按。 */
let forceSelect = localStorage.getItem("sshpro.forcesel") !== "0";
const IS_MAC = ["Macintosh", "MacIntel", "MacPPC", "Mac68K"]
  .includes(navigator.platform);
const FORCE_PROP = IS_MAC ? "altKey" : "shiftKey";

/* ---------- 会话 ---------- */

let sessions = [];
let active = null;

function tabLabel(name) {
  if (name === "sshpro") return "终端 1";
  const m = name.match(/^sshpro-(\d+)$/);
  return m ? `终端 ${m[1]}` : name;
}

class Session {
  constructor(name, ws) {
    this.name = name;
    this.ws = ws;
    this.dead = false;
    this.destroying = false;
    this.cwd = null;          // 终端当前目录（后端轮询推送）
    this.fsPath = null;       // 文件面板当前目录
    this.fsEntries = null;
    this.fsError = null;
    this.fsFollow = true;

    this.holder = document.createElement("div");
    this.holder.className = "term-holder hidden";
    $("#terms").appendChild(this.holder);

    this.term = new Terminal({
      theme: TERM_THEME,
      fontSize: 14,
      fontFamily: 'ui-monospace, "Cascadia Code", "JetBrains Mono", Consolas, "Noto Sans Mono CJK SC", monospace',
      cursorBlink: true,
      scrollback: 10000,
      allowProposedApi: true,
      altClickMovesCursor: false,
      // macOS 上强制划选走 Option 修饰键，且必须显式打开这个开关
      macOptionClickForcesSelection: true,
    });
    this.fit = new FitAddon.FitAddon();
    this.term.loadAddon(this.fit);
    this.term.open(this.holder);

    this.term.onData((data) => {
      if (!this.dead && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: "data", data }));
      }
    });

    // 强制选择：捕获阶段给鼠标事件打上修饰键标记，xterm 便始终走本地划选
    const forceMod = (e) => {
      if (forceSelect && !e[FORCE_PROP]) {
        Object.defineProperty(e, FORCE_PROP, { get: () => true });
      }
    };
    for (const type of ["mousedown", "mousemove", "mouseup", "dblclick"]) {
      this.holder.addEventListener(type, forceMod, true);
    }

    // 选中即复制（拖选结束后自动写入剪贴板）
    this._selTimer = null;
    this.term.onSelectionChange(() => {
      clearTimeout(this._selTimer);
      this._selTimer = setTimeout(() => {
        const sel = this.term.getSelection();
        if (sel) copyText(sel);
      }, 300);
    });

    // 有选中内容时 Cmd+C / Ctrl+C = 复制（走我们自己的可靠路径），
    // 无选中时 Ctrl+C 照常发送中断
    this.term.attachCustomKeyEventHandler((e) => {
      if (e.type === "keydown" && (e.ctrlKey || e.metaKey) && !e.altKey &&
          (e.key === "c" || e.key === "C") && this.term.hasSelection()) {
        copyText(this.term.getSelection());
        this.term.clearSelection();
        return false;
      }
      return true;
    });

    // OSC 7：若 shell 配置了目录上报，实时同步（比后端轮询更快）
    this.term.parser.registerOscHandler(7, (data) => {
      try {
        const u = new URL(data);
        if (u.protocol === "file:") {
          this.onCwd(decodeURIComponent(u.pathname) || "/");
        }
      } catch { /* 忽略无效 OSC */ }
      return true;
    });

    this.resizeObs = new ResizeObserver(() => this.refit());
    this.resizeObs.observe(this.holder);

    this.tab = document.createElement("div");
    this.tab.className = "tab";
    this.tab.innerHTML =
      `<span class="dot"></span><span class="label"></span><button class="close" title="关闭标签（会话保留，可随时恢复）">×</button>`;
    this.tab.querySelector(".label").textContent = tabLabel(name);
    this.tab.addEventListener("click", () => {
      if (this.dead) {          // 点击已断开的标签 => 重连同名会话
        const n = this.name;
        this.dispose(true);
        openSession(n);
      } else {
        this.activate();
      }
    });
    this.tab.querySelector(".close").addEventListener("click", (e) => {
      e.stopPropagation();
      this.dispose();
    });
    $("#tabs").appendChild(this.tab);

    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.onClosed();
    ws.onerror = () => this.onClosed();
  }

  onMessage(ev) {
    if (ev.data instanceof ArrayBuffer) {
      this.term.write(new Uint8Array(ev.data));
      return;
    }
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "cwd") {
      this.onCwd(msg.path);
    } else if (msg.type === "closed") {
      this.onClosed();
    }
  }

  onCwd(path) {
    this.cwd = path;
    if (this.fsFollow && path && path !== this.fsPath) {
      fpNavigate(this, path);
    }
  }

  onClosed() {
    if (this.destroying) {   // 手动销毁：直接移除标签
      this.dispose(true);
      toast(`${tabLabel(this.name)} 已销毁`);
      return;
    }
    if (this.dead) return;
    this.dead = true;
    this.tab.classList.add("dead");
    this.term.write(
      "\r\n\x1b[1;33m⏸ 连接已断开\x1b[0m —— 会话仍在后台运行，点击此标签即可重连\r\n");
  }

  refit() {
    if (this.holder.classList.contains("hidden")) return;
    try { this.fit.fit(); } catch { return; }
    if (!this.dead && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({
        type: "resize", cols: this.term.cols, rows: this.term.rows,
      }));
    }
  }

  activate() {
    active = this;
    localStorage.setItem("sshpro.active", this.name);
    sessions.forEach((s) => {
      s.tab.classList.toggle("active", s === this);
      s.holder.classList.toggle("hidden", s !== this);
    });
    $("#empty").hidden = true;
    this.refit();
    this.term.focus();
    renderFiles(this);
    if (!this.fsEntries && !this.fsError) fpNavigate(this, this.cwd || "");
  }

  dispose(quiet) {
    this.dead = true;
    try { this.ws.close(); } catch { /* noop */ }
    this.resizeObs.disconnect();
    this.term.dispose();
    this.holder.remove();
    this.tab.remove();
    sessions = sessions.filter((s) => s !== this);
    if (active === this) {
      active = null;
      if (sessions.length) {
        sessions[sessions.length - 1].activate();
      } else if (!quiet) {
        showEmpty();
      }
    }
    if (!sessions.length && !quiet) showEmpty();
  }
}

function showEmpty() {
  $("#empty").hidden = false;
  clearFiles();
}

/* ---------- 会话打开 / 新建 / 销毁 ---------- */

function openSession(name, onFail) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  let settled = false;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "auth", session: name, cols: 80, rows: 24 }));
  };
  ws.onmessage = (ev) => {
    if (settled || ev.data instanceof ArrayBuffer) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ready") {
      settled = true;
      if (msg.tmux === false) {
        toast("服务器未安装 tmux，此会话断线后无法恢复", true);
      }
      const s = new Session(name, ws);
      sessions.push(s);
      const wanted = localStorage.getItem("sshpro.active");
      if (!active || name === wanted) s.activate();
    } else if (msg.type === "error") {
      settled = true;
      toast(msg.message || "会话打开失败", true);
      if (onFail) onFail();
    }
  };
  const fail = (text) => () => {
    if (!settled) {
      settled = true;
      toast(text, true);
      if (onFail) onFail();
    }
  };
  ws.onerror = fail("无法连接 sshpro 服务");
  ws.onclose = fail("连接被服务端关闭");
}

async function newSession() {
  const taken = new Set(sessions.map((s) => s.name));
  try {
    const r = await fetch("/api/sessions");
    (await r.json()).sessions.forEach((n) => taken.add(n));
  } catch { /* 服务器列表拿不到就只按本地开着的算 */ }
  let name = "sshpro";
  for (let i = 2; taken.has(name); i++) name = `sshpro-${i}`;
  openSession(name);
}

function destroyActive() {
  if (!active) return;
  const s = active;
  const label = tabLabel(s.name);
  if (!confirm(`彻底销毁 ${label}？其中正在运行的程序都会被终止，且无法恢复。`)) return;
  s.destroying = true;
  fetch(`/api/kill?name=${encodeURIComponent(s.name)}`, { method: "POST" })
    .then((r) => r.json())
    .then((d) => {
      if (!d.ok) { s.destroying = false; toast("销毁失败", true); }
      // 成功时 tmux 会话被杀，PTY 退出 -> onClosed 走 destroying 分支移除标签
    })
    .catch(() => { s.destroying = false; toast("销毁请求失败", true); });
}

/* ---------- 文件面板 ---------- */

function fpParent(p) {
  const q = (p || "/").replace(/\/+$/, "");
  const i = q.lastIndexOf("/");
  return i <= 0 ? "/" : q.slice(0, i);
}

function fpJoin(dir, name) {
  return (dir.endsWith("/") ? dir : dir + "/") + name;
}

async function fpNavigate(s, path, manual) {
  if (manual) s.fsFollow = false;
  try {
    const r = await fetch(`/api/fs/list?path=${encodeURIComponent(path || "")}`);
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    s.fsPath = data.path;
    s.fsEntries = data.entries;
    s.fsError = null;
  } catch (e) {
    s.fsError = e.message;
  }
  if (active === s) renderFiles(s);
}

function clearFiles() {
  $("#fp-path").textContent = "—";
  $("#fp-path").title = "";
  $("#fp-list").innerHTML = "";
  $("#fp-follow").classList.remove("on");
}

function renderFiles(s) {
  if (!s) { clearFiles(); return; }
  $("#fp-path").textContent = s.fsPath || "—";
  $("#fp-path").title = s.fsPath || "";
  $("#fp-follow").classList.toggle("on", s.fsFollow);
  const list = $("#fp-list");
  list.innerHTML = "";
  if (s.fsError) {
    const err = document.createElement("div");
    err.className = "fp-error";
    err.textContent = s.fsError;
    list.appendChild(err);
    return;
  }
  if (!s.fsEntries) return;
  if (!s.fsEntries.length) {
    const empty = document.createElement("div");
    empty.className = "fp-error";
    empty.textContent = "（空目录）";
    list.appendChild(empty);
    return;
  }
  const frag = document.createDocumentFragment();
  for (const ent of s.fsEntries) {
    const full = fpJoin(s.fsPath, ent.name);
    const row = document.createElement("div");
    row.className = "fp-item" + (ent.dir ? " dir" : "");
    const ico = document.createElement("span");
    ico.className = "fp-ico";
    ico.textContent = ent.dir ? "📁" : "📄";
    const name = document.createElement("span");
    name.className = "fp-name";
    name.textContent = ent.name;
    name.title = ent.name;
    const size = document.createElement("span");
    size.className = "fp-size";
    size.textContent = ent.dir ? "" : fmtBytes(ent.size);
    row.append(ico, name, size);
    if (ent.dir) {
      row.addEventListener("click", () => fpNavigate(s, full, true));
    } else {
      const act = document.createElement("span");
      act.className = "fp-act";
      const d = document.createElement("button");
      d.className = "fp-btn";
      d.textContent = "⬇";
      d.title = "下载";
      d.addEventListener("click", (e) => { e.stopPropagation(); download(full); });
      act.appendChild(d);
      row.appendChild(act);
      // 点击任意文件尝试在线查看：文本/二进制由服务端按内容判断
      row.classList.add("viewable");
      row.addEventListener("click", () => openViewer(full));
    }
    frag.appendChild(row);
  }
  list.appendChild(frag);
}

function download(path) {
  const a = document.createElement("a");
  a.href = `/api/fs/download?path=${encodeURIComponent(path)}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function openViewer(path) {
  $("#viewer").classList.remove("hidden");
  $("#viewer-title").textContent = path;
  $("#viewer-title").title = path;
  $("#viewer-note").hidden = true;
  const body = $("#viewer-body");
  body.textContent = "加载中…";
  $("#viewer-dl").onclick = () => download(path);
  try {
    const r = await fetch(`/api/fs/view?path=${encodeURIComponent(path)}`);
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${r.status}`);
    }
    const skipped = Number(r.headers.get("X-Sshpro-Skipped") || 0);
    body.textContent = await r.text();
    if (skipped > 0) {
      const note = $("#viewer-note");
      note.textContent = `文件较大，已跳过开头 ${fmtBytes(skipped)}，仅显示末尾部分`;
      note.hidden = false;
      body.scrollTop = body.scrollHeight;   // 日志默认看最新
    } else {
      body.scrollTop = 0;
    }
  } catch (e) {
    body.textContent = "加载失败：" + e.message;
  }
}

function closeViewer() {
  $("#viewer").classList.add("hidden");
  if (active) active.term.focus();
}

/* ---------- 事件绑定 ---------- */

$("#btn-new").addEventListener("click", newSession);
$("#btn-empty-new").addEventListener("click", newSession);
$("#btn-destroy").addEventListener("click", destroyActive);

$("#btn-copy").addEventListener("click", () => {
  forceSelect = !forceSelect;
  localStorage.setItem("sshpro.forcesel", forceSelect ? "1" : "0");
  $("#btn-copy").classList.toggle("on", forceSelect);
  toast(forceSelect ? "强制选中复制：开" : "强制选中复制：关（鼠标交还远端程序）");
  if (active) active.term.focus();
});
if (forceSelect) $("#btn-copy").classList.add("on");

$("#btn-files").addEventListener("click", () => {
  const hidden = $("#files").classList.toggle("hidden");
  $("#btn-files").classList.toggle("on", !hidden);
  localStorage.setItem("sshpro.files", hidden ? "0" : "1");
  if (active) active.refit();
});
if (localStorage.getItem("sshpro.files") === "0") {
  $("#files").classList.add("hidden");
} else {
  $("#btn-files").classList.add("on");
}

$("#fp-up").addEventListener("click", () => {
  if (active && active.fsPath) fpNavigate(active, fpParent(active.fsPath), true);
});
$("#fp-refresh").addEventListener("click", () => {
  if (active) fpNavigate(active, active.fsPath || "");
});
$("#fp-follow").addEventListener("click", () => {
  if (!active) return;
  active.fsFollow = !active.fsFollow;
  if (active.fsFollow && active.cwd) fpNavigate(active, active.cwd);
  else renderFiles(active);
});

$("#viewer-close").addEventListener("click", closeViewer);
$("#viewer").addEventListener("click", (e) => {
  if (e.target === $("#viewer")) closeViewer();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#viewer").classList.contains("hidden")) closeViewer();
});

/* ---------- 启动：恢复所有 tmux 会话 ---------- */

async function init() {
  clearFiles();
  let names = [];
  try {
    const r = await fetch("/api/sessions");
    const data = await r.json();
    names = data.sessions || [];
    if (!data.tmux) toast("服务器未安装 tmux，会话断线后无法恢复", true);
  } catch {
    toast("无法获取会话列表", true);
  }
  if (!names.length) names = ["sshpro"];
  names.forEach((n) => openSession(n));
}

init();
