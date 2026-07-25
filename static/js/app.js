/* sshpro 前端：标签页会话管理 + xterm.js 终端 + 系统监控面板 */
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

/* ---------- 格式化工具 ---------- */

function fmtBytes(n) {
  if (!isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + units[i];
}

function fmtRate(n) { return fmtBytes(n) + "/s"; }

function fmtUptime(sec) {
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分钟`;
  return `${m} 分钟`;
}

function setBar(el, pct) {
  el.style.width = Math.min(Math.max(pct, 0), 100) + "%";
  el.className = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
}

/* ---------- 剪贴板 ---------- */

function copyFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* noop */ }
  ta.remove();
  if (active) active.term.focus();
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(() => copyFallback(text));
  } else {
    copyFallback(text);
  }
}

/* 强制选择模式：即使远端程序（vim/htop/tmux 等）接管了鼠标，
   划词依然走本地选择——原理是给鼠标事件强行加上 Shift（macOS 为 Option），
   等效于终端里常用的 Shift+拖选，但不需要真的按住。 */
let forceSelect = localStorage.getItem("sshpro.forcesel") !== "0";
const FORCE_PROP = /Mac/.test(navigator.platform) ? "altKey" : "shiftKey";

/* ---------- 最近连接记录（不含密码） ---------- */

function loadRecents() {
  try { return JSON.parse(localStorage.getItem("sshpro.recents")) || []; }
  catch { return []; }
}

function saveRecent(host, port, username) {
  const list = loadRecents().filter((r) => r.host !== host);
  list.unshift({ host, port, username });
  localStorage.setItem("sshpro.recents", JSON.stringify(list.slice(0, 10)));
  renderRecents();
}

function renderRecents() {
  $("#recent-hosts").innerHTML = loadRecents()
    .map((r) => `<option value="${r.host}">${r.username}@${r.host}:${r.port}</option>`)
    .join("");
}

/* ---------- 会话 ---------- */

let sessions = [];
let active = null;

class Session {
  constructor(opts, ws) {
    this.opts = opts;
    this.ws = ws;
    this.dead = false;
    this.sysinfo = null;
    this.stats = null;

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
      // macOS 上强制划选依赖 Option 修饰键，且必须显式打开这个开关，
      // 否则注入的 altKey 不生效（Windows/Linux 走 shiftKey，无需开关）
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
    const forceShift = (e) => {
      if (forceSelect && !e[FORCE_PROP]) {
        Object.defineProperty(e, FORCE_PROP, { get: () => true });
      }
    };
    for (const type of ["mousedown", "mousemove", "mouseup", "dblclick"]) {
      this.holder.addEventListener(type, forceShift, true);
    }

    // 选中即复制（拖选结束后自动写入剪贴板）
    this._selTimer = null;
    this.term.onSelectionChange(() => {
      clearTimeout(this._selTimer);
      this._selTimer = setTimeout(() => {
        const sel = this.term.getSelection();
        if (sel) copyText(sel);
      }, 250);
    });

    // 有选中内容时 Ctrl+C = 复制（不发 SIGINT），无选中时照常中断
    this.term.attachCustomKeyEventHandler((e) => {
      if (e.type === "keydown" && e.ctrlKey && !e.altKey &&
          (e.key === "c" || e.key === "C") && this.term.hasSelection()) {
        copyText(this.term.getSelection());
        this.term.clearSelection();
        return false;
      }
      return true;
    });

    this.resizeObs = new ResizeObserver(() => this.refit());
    this.resizeObs.observe(this.holder);

    this.tab = document.createElement("div");
    this.tab.className = "tab";
    this.tab.innerHTML =
      `<span class="dot"></span><span class="label"></span><button class="close" title="关闭">×</button>`;
    this.tab.querySelector(".label").textContent =
      opts.local ? "本机" : `${opts.username}@${opts.host}`;
    this.tab.addEventListener("click", () => this.activate());
    this.tab.querySelector(".close").addEventListener("click", (e) => {
      e.stopPropagation();
      this.dispose();
    });
    $("#tabs").appendChild(this.tab);

    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onclose = () => this.markDead();
    ws.onerror = () => this.markDead();
  }

  onMessage(ev) {
    if (ev.data instanceof ArrayBuffer) {
      this.term.write(new Uint8Array(ev.data));
      return;
    }
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "sysinfo") {
      this.sysinfo = msg;
      if (this.opts.local && msg.hostname) {
        this.tab.querySelector(".label").textContent = "本机 · " + msg.hostname;
      }
      if (active === this) renderSysinfo(this);
    } else if (msg.type === "stats") {
      this.stats = msg;
      if (active === this) renderStats(this);
    } else if (msg.type === "closed") {
      this.markDead();
    }
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
    sessions.forEach((s) => {
      s.tab.classList.toggle("active", s === this);
      s.holder.classList.toggle("hidden", s !== this);
    });
    this.refit();
    this.term.focus();
    renderSysinfo(this);
    renderStats(this);
  }

  markDead() {
    if (this.dead) return;
    this.dead = true;
    this.tab.classList.add("dead");
    this.term.write(this.opts.local
      ? "\r\n\x1b[1;33m⏸ 连接已断开\x1b[0m —— 后台命令仍在 tmux 会话中运行，刷新页面即可接回\r\n"
      : "\r\n\x1b[1;31m✖ 连接已断开\x1b[0m（关闭标签页后可重新连接）\r\n");
  }

  dispose() {
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
      } else {
        clearMonitor();
        showLogin(false);
      }
    }
  }
}

/* ---------- 监控面板渲染 ---------- */

function clearMonitor() {
  renderSysinfo(null);
  renderStats(null);
}

function renderSysinfo(s) {
  const info = s && s.sysinfo;
  $("#mon-hostname").textContent = info ? info.hostname : "—";
  $("#mon-os").textContent = info ? info.os : "—";
  $("#mon-kernel").textContent = info ? info.kernel : "—";
  $("#mon-cores").textContent = info ? info.cores : "—";
}

function renderStats(s) {
  const st = s && s.stats;
  $("#mon-uptime").textContent = st && st.uptime != null ? fmtUptime(st.uptime) : "—";

  const cpu = st && st.cpu != null ? st.cpu : null;
  $("#mon-cpu-pct").textContent = cpu != null ? cpu.toFixed(1) + "%" : "—";
  setBar($("#mon-cpu-bar"), cpu != null ? cpu : 0);
  $("#mon-load").textContent = st && st.load ? st.load.join("  ") : "—";

  if (st && st.mem && st.mem.total > 0) {
    const pct = (100 * st.mem.used) / st.mem.total;
    $("#mon-mem-pct").textContent = pct.toFixed(1) + "%";
    setBar($("#mon-mem-bar"), pct);
    $("#mon-mem-txt").innerHTML =
      `<b>${fmtBytes(st.mem.used)}</b> / ${fmtBytes(st.mem.total)}`;
  } else {
    $("#mon-mem-pct").textContent = "—";
    setBar($("#mon-mem-bar"), 0);
    $("#mon-mem-txt").textContent = "—";
  }

  if (st && st.swap && st.swap.total > 0) {
    const pct = (100 * st.swap.used) / st.swap.total;
    $("#mon-swap-pct").textContent = pct.toFixed(1) + "%";
    setBar($("#mon-swap-bar"), pct);
  } else {
    $("#mon-swap-pct").textContent = st ? "未启用" : "—";
    setBar($("#mon-swap-bar"), 0);
  }

  const disksEl = $("#mon-disks");
  if (st && st.disks && st.disks.length) {
    disksEl.innerHTML = st.disks.map((d) => {
      const pct = d.total > 0 ? (100 * d.used) / d.total : 0;
      const cls = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
      return `<div class="disk-item">
        <div class="disk-head">
          <span class="mount" title="${d.mount}">${d.mount}</span>
          <span class="size">${fmtBytes(d.used)} / ${fmtBytes(d.total)}</span>
        </div>
        <div class="bar slim"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></div>
      </div>`;
    }).join("");
  } else {
    disksEl.innerHTML = '<div class="mon-sub">—</div>';
  }

  $("#mon-rx").textContent = st && st.net ? fmtRate(st.net.rx) : "—";
  $("#mon-tx").textContent = st && st.net ? fmtRate(st.net.tx) : "—";
}

/* ---------- 登录 / 连接流程 ---------- */

function connectLocal() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  let settled = false;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "auth", local: true, cols: 80, rows: 24 }));
  };
  ws.onmessage = (ev) => {
    if (settled || ev.data instanceof ArrayBuffer) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ready") {
      settled = true;
      hideLogin();
      const session = new Session({ local: true }, ws);
      sessions.push(session);
      session.activate();
    } else if (msg.type === "error") {
      settled = true;
      showLogin(sessions.length > 0);
      loginError(msg.message || "本机会话不可用");
    }
  };
  const fail = (text) => () => {
    if (!settled) { settled = true; showLogin(sessions.length > 0); loginError(text); }
  };
  ws.onerror = fail("无法连接 sshpro 服务");
  ws.onclose = fail("连接被服务端关闭");
}

function showLogin(cancellable) {
  $("#login-overlay").classList.remove("hidden");
  $("#btn-cancel").hidden = !cancellable;
  $("#login-error").hidden = true;
  const recents = loadRecents();
  if (recents.length && !$("#f-host").value) {
    $("#f-host").value = recents[0].host;
    $("#f-port").value = recents[0].port;
    $("#f-user").value = recents[0].username;
  }
  $("#f-pass").value = "";
  ($("#f-host").value ? $("#f-pass") : $("#f-host")).focus();
}

function hideLogin() {
  $("#login-overlay").classList.add("hidden");
  $("#app").hidden = false;
}

function loginError(text) {
  const el = $("#login-error");
  el.textContent = text;
  el.hidden = false;
  $("#btn-connect").disabled = false;
  $("#btn-connect").textContent = "连 接";
}

function connect(opts) {
  $("#btn-connect").disabled = true;
  $("#btn-connect").textContent = "连接中…";

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  let settled = false;

  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "auth",
      host: opts.host, port: opts.port,
      username: opts.username, password: opts.password,
      cols: 80, rows: 24,
    }));
  };

  ws.onmessage = (ev) => {
    if (settled || ev.data instanceof ArrayBuffer) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ready") {
      settled = true;
      saveRecent(opts.host, opts.port, opts.username);
      hideLogin();
      $("#btn-connect").disabled = false;
      $("#btn-connect").textContent = "连 接";
      const session = new Session(opts, ws);
      sessions.push(session);
      session.activate();
    } else if (msg.type === "error") {
      settled = true;
      loginError(msg.message || "连接失败");
    }
  };

  ws.onerror = () => { if (!settled) { settled = true; loginError("无法连接 sshpro 服务"); } };
  ws.onclose = () => { if (!settled) { settled = true; loginError("连接被服务端关闭"); } };
}

/* ---------- 事件绑定 ---------- */

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  connect({
    host: $("#f-host").value.trim(),
    port: parseInt($("#f-port").value, 10) || 22,
    username: $("#f-user").value.trim(),
    password: $("#f-pass").value,
  });
});

$("#f-host").addEventListener("input", () => {
  const r = loadRecents().find((x) => x.host === $("#f-host").value.trim());
  if (r) { $("#f-port").value = r.port; $("#f-user").value = r.username; }
});

$("#btn-new").addEventListener("click", () => showLogin(true));
$("#btn-cancel").addEventListener("click", () => {
  $("#login-overlay").classList.add("hidden");
  if (active) active.term.focus();
});
$("#btn-local").addEventListener("click", () => connectLocal());

$("#btn-copy").addEventListener("click", () => {
  forceSelect = !forceSelect;
  localStorage.setItem("sshpro.forcesel", forceSelect ? "1" : "0");
  $("#btn-copy").classList.toggle("on", forceSelect);
  if (active) active.term.focus();
});
if (forceSelect) $("#btn-copy").classList.add("on");

$("#btn-monitor").addEventListener("click", () => {
  const hidden = $("#monitor").classList.toggle("hidden");
  $("#btn-monitor").classList.toggle("on", !hidden);
  localStorage.setItem("sshpro.monitor", hidden ? "0" : "1");
  if (active) active.refit();
});

if (localStorage.getItem("sshpro.monitor") === "0") {
  $("#monitor").classList.add("hidden");
} else {
  $("#btn-monitor").classList.add("on");
}

window.addEventListener("beforeunload", (e) => {
  // 本机会话跑在 tmux 里，关页面不丢任务，无需拦截
  if (sessions.some((s) => !s.dead && !s.opts.local)) e.preventDefault();
});

renderRecents();
clearMonitor();
connectLocal();
