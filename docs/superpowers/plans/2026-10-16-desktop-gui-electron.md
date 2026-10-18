# dots 桌面界面 · 计划二：Electron 壳与界面实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出那个窗口 —— 双击起来能看到它、能跟它说话、能下目标、能批审批、每条命令都有中文按钮，并且关窗口就干净退出。

**Architecture:** Electron 主进程起一个 `python -m dots bridge` 子进程，双方在 stdin/stdout 上用一行一个 JSON 说话（协议已定稿，见 spec §4）。主进程只管进程与窗口，界面用原生 HTML/CSS/JS（无构建步骤），所有视觉参数走 CSS 变量。

**Tech Stack:** Electron（主进程 Node 内置模块：`child_process` / `readline` / `path`）、原生 HTML/CSS/JS、`node --test`（Node 内置测试器，只测纯逻辑）。

**依据：** `docs/superpowers/specs/2026-10-16-electron-desktop-gui-design.md`（协议在 §4，界面在 §5，错误处理在 §6）

**前置：** 计划一（Python 后端）已完成，`python -m dots bridge` 能跑。

---

## 提交说明

用户明确要求**本分支暂不提交**。所有「提交」步骤一律跳过。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `desktop/package.json` | 依赖、脚本、electron-builder 配置（打包在计划三里用） |
| `desktop/electron/protocol.js` | **纯函数**：行 → 消息分类；请求 id 配对表。可单测 |
| `desktop/electron/backend.js` | 子进程管理：spawn / 事件 / 10 秒优雅退出 / `resolveBackend()` |
| `desktop/electron/main.js` | 窗口 + IPC 转发 + 应用生命周期 |
| `desktop/electron/preload.js` | `contextBridge` 白名单，不暴露原始 `ipcRenderer` |
| `desktop/renderer/index.html` | 骨架 + CSP |
| `desktop/renderer/theme.css` | **设计变量**（主题的落点，本期不做导入 UI） |
| `desktop/renderer/style.css` | 布局与组件，只引用变量 |
| `desktop/renderer/app.js` | store + 渲染，无框架 |
| `desktop/electron/protocol.test.js` | `node --test` 跑的纯逻辑测试 |

---

## Task 1: 脚手架 —— 能起一个空窗口

**Files:**
- Create: `desktop/package.json`
- Create: `desktop/electron/main.js`（本任务先放最小版本，Task 4 补全）
- Create: `desktop/renderer/index.html`（本任务先放最小版本）

- [ ] **Step 1: 建 package.json**

先看当前 Electron 版本，别写死过时的：

```bash
cd desktop && npm view electron version
```

然后按拿到的版本写（下面的大版本号以实际为准）：

```json
{
  "name": "dots-desktop",
  "version": "0.1.0",
  "private": true,
  "description": "dots 的桌面界面",
  "main": "electron/main.js",
  "scripts": {
    "start": "electron .",
    "test": "node --test electron/"
  },
  "devDependencies": {
    "electron": "^38.0.0"
  }
}
```

`npm install`。**装完把 `desktop/node_modules/` 确认在 .gitignore 里**（已经在仓库根的 .gitignore 里了）。

- [ ] **Step 2: 最小 main.js**

```js
// 主进程:开一个窗口。子进程管理和 IPC 在 Task 3/4 补。
const { app, BrowserWindow } = require("electron");
const path = require("node:path");

function createWindow() {
  const win = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 720,
    minHeight: 520,
    backgroundColor: "#161A20",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // 先不显示,等首屏画好再露 —— 否则会闪一下白底
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
  return win;
}

app.whenReady().then(createWindow);
app.on("window-all-closed", () => app.quit());
```

- [ ] **Step 3: 最小 index.html**

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:">
  <title>dots</title>
  <link rel="stylesheet" href="theme.css">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div id="app">正在启动…</div>
  <script src="app.js"></script>
</body>
</html>
```

`theme.css` / `style.css` / `app.js` 各放一个空文件（`app.js` 先写 `document.getElementById("app").textContent = "dots";`）。

- [ ] **Step 4: 起一下**

Run: `cd desktop && npm start`
Expected: 窗口起来，深色底，写着 `dots`。关掉窗口进程退出。

注意 `preload.js` 还不存在 —— Electron 会警告但不致命。Task 4 补。

---

## Task 2: `protocol.js` —— 纯函数，可单测

**为什么单独一个文件：** 这是唯一能脱离 Electron 单测的部分（`node --test` 不需要显示器、不需要装 Electron），也是最容易出错的部分（id 配对、超时、退出时清理）。

**Files:**
- Create: `desktop/electron/protocol.js`
- Test: `desktop/electron/protocol.test.js`

- [ ] **Step 1: 写失败的测试**

```js
// desktop/electron/protocol.test.js
const test = require("node:test");
const assert = require("node:assert");
const { classify, Pending } = require("./protocol");

test("分类:应答 / 事件 / 垃圾", () => {
  // 有 id 且有 ok = 应答
  assert.deepEqual(
    classify('{"id":1,"ok":true,"data":{"gid":"43e86d"}}'),
    { kind: "reply", id: 1, ok: true, data: { gid: "43e86d" } },
  );
  assert.deepEqual(
    classify('{"id":2,"ok":false,"error":"没有叫 nope 的 bot"}'),
    { kind: "reply", id: 2, ok: false, error: "没有叫 nope 的 bot" },
  );
  // 有 event = 事件
  assert.deepEqual(
    classify('{"event":"state","goals":[]}'),
    { kind: "event", name: "state", payload: { event: "state", goals: [] } },
  );
  // 后端对非法 JSON 行的报错:既没有 id 也没有 event —— 第三种形状,
  // 不能当成应答,也不能当成事件,单独一类。
  assert.deepEqual(
    classify('{"ok":false,"error":"这行不是合法 JSON"}'),
    { kind: "junk", error: "这行不是合法 JSON" },
  );
});

test("分类:空行 / 半个 JSON / 非对象", () => {
  assert.equal(classify(""), null);
  assert.equal(classify("   "), null);
  assert.equal(classify('{"id":1,'), null);        // 解析不了,当噪声丢掉
  assert.equal(classify('"just a string"'), null); // 不是对象
  assert.equal(classify("[1,2]"), null);
});

test("配对:按 id resolve,失败 reject", async () => {
  const p = new Pending();
  const promise = p.wait(7, 1000);

  assert.equal(p.settle({ id: 7, ok: true, data: { code: 0 } }), true);
  assert.deepEqual(await promise, { code: 0 });

  const failing = p.wait(8, 1000);
  p.settle({ id: 8, ok: false, error: "boom" });
  await assert.rejects(() => failing, /boom/);
});

test("配对:超时会 reject 并清掉,不泄漏", async () => {
  const p = new Pending();
  const promise = p.wait(9, 30);
  await assert.rejects(() => promise, /超时/);
  assert.equal(p.size, 0, "超时之后还留着,下次同 id 会串");
});

test("配对:迟到或陌生的 id 不会炸", () => {
  const p = new Pending();
  assert.equal(p.settle({ id: 999, ok: true }), false);
});

test("退出时把所有在等的一起 reject", async () => {
  const p = new Pending();
  const a = p.wait(1, 60000);
  const b = p.wait(2, 60000);
  p.rejectAll("后端没了");
  await assert.rejects(() => a, /后端没了/);
  await assert.rejects(() => b, /后端没了/);
  assert.equal(p.size, 0);
});
```

- [ ] **Step 2: 跑,确认失败**

Run: `cd desktop && npm test`
Expected: FAIL —— `Cannot find module './protocol'`

- [ ] **Step 3: 实现**

```js
// protocol.js —— 协议里唯一能脱离 Electron 单测的部分。
//
// 三句话概括协议:一行一个 JSON;带 id 的按 id 配对(请求/应答);
// 带 event 的是主动推送,不用配对。
//
// 有个第三种形状要当心:后端收到非法 JSON 行时回的是
// {"ok":false,"error":...} —— 没有 id 也没有 event。当成应答会配不上,
// 当成事件会没名字,所以单独一类。

/** 一行文本 -> 分类后的消息。解析不了 / 空行返回 null(调用方丢掉)。 */
function classify(line) {
  const text = String(line ?? "").trim();
  if (!text) return null;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return null;                 // 不是 JSON,当噪声
  }
  if (msg === null || typeof msg !== "object" || Array.isArray(msg)) return null;

  if (typeof msg.event === "string") {
    return { kind: "event", name: msg.event, payload: msg };
  }
  if ("id" in msg && "ok" in msg) {
    return msg.ok
      ? { kind: "reply", id: msg.id, ok: true, data: msg.data ?? {} }
      : { kind: "reply", id: msg.id, ok: false, error: msg.error ?? "" };
  }
  return { kind: "junk", error: msg.error ?? "看不懂的一行" };
}

/** 请求 id -> {resolve, reject} 的配对表。 */
class Pending {
  #map = new Map();
  #seq = 0;

  get size() { return this.#map.size; }

  /** 下一个请求 id。从 1 开始,和界面上的按钮对应起来好读日志。 */
  nextId() { return ++this.#seq; }

  /** 等一个应答。超时会 reject 并把这条清掉 —— 不清的话同 id 会串。 */
  wait(id, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#map.delete(id);
        reject(new Error(`后端 ${timeoutMs}ms 没回应`));
      }, timeoutMs);
      this.#map.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
  }

  /** 拿一条应答去兑现。没人等这个 id 就返回 false(迟到/陌生,不是错误)。 */
  settle(reply) {
    const entry = this.#map.get(reply.id);
    if (!entry) return false;
    this.#map.delete(reply.id);
    if (reply.ok) entry.resolve(reply.data);
    else entry.reject(new Error(reply.error));
    return true;
  }

  /** 后端没了 —— 把所有在等的一起 reject,不然界面永远转圈。 */
  rejectAll(reason) {
    for (const [, entry] of this.#map) entry.reject(new Error(reason));
    this.#map.clear();
  }
}

module.exports = { classify, Pending };
```

- [ ] **Step 4: 跑,确认通过**

Run: `cd desktop && npm test`
Expected: 6 个 test 全过

---

## Task 3: `backend.js` —— 子进程与协议客户端

**Files:**
- Create: `desktop/electron/backend.js`
- Test: `desktop/electron/backend.test.js`（只测 `resolveBackend` 与命令行拼装，起真进程的部分在 Task 10 手工验）

- [ ] **Step 1: 写失败的测试**

```js
const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const { resolveBackend, buildArgs } = require("./backend");

test("找 Python:顺序是 环境变量 > 项目 venv > PATH", () => {
  const root = path.join("C:", "proj");
  // DOTS_PYTHON 优先
  assert.equal(
    resolveBackend({ root, env: { DOTS_PYTHON: "/custom/python" }, exists: () => true }).cmd,
    "/custom/python",
  );
  // 其次项目 venv
  const venv = path.join(root, "desktop", ".venv", "Scripts", "python.exe");
  assert.equal(
    resolveBackend({ root, env: {}, exists: (p) => p === venv }).cmd,
    venv,
  );
  // 都没有就退回 PATH 上的 python
  assert.equal(resolveBackend({ root, env: {}, exists: () => false }).cmd, "python");
});

test("命令行:默认自启循环,--no-loop 才关", () => {
  assert.deepEqual(buildArgs({ configPath: null }), ["-m", "dots", "bridge"]);
  assert.deepEqual(buildArgs({ configPath: null, noLoop: true }),
    ["-m", "dots", "bridge", "--no-loop"]);
  assert.deepEqual(buildArgs({ configPath: "C:/x/dot.toml" }),
    ["-m", "dots", "bridge", "-c", "C:/x/dot.toml"]);
});
```

- [ ] **Step 2: 跑,确认失败**（`Cannot find module './backend'`）

- [ ] **Step 3: 实现**

```js
// backend.js —— 管那个 python 子进程,以及和它说话。
//
// 它是整个界面里唯一知道「怎么找到 Python」的地方。以后做「自带运行时
// 发给别人」,只改 resolveBackend 一个函数(指到安装包里的
// resources/python/python.exe),别的都不用动。

const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const { classify, Pending } = require("./protocol");

// 关窗口时:先发 quit 让它优雅退出(复位 running 目标),最多等这么久再 kill。
// 为什么是 10 秒不是 2 秒:shutdown 要 join 三条线程(每条最多 3 秒),
// 然后才复位目标。2 秒 kill 会把复位整个跳过 —— 目标留在 running 等 1800 秒。
const EXIT_GRACE_MS = 10000;

/** 用哪个 python、带什么参数。抽成纯函数是为了能单测。 */
function resolveBackend({ root, env = process.env, exists = fs.existsSync } = {}) {
  if (env.DOTS_PYTHON) return { cmd: env.DOTS_PYTHON, cwd: root };
  const venv = path.join(root, "desktop", ".venv", "Scripts", "python.exe");
  if (exists(venv)) return { cmd: venv, cwd: root };
  return { cmd: "python", cwd: root };
}

function buildArgs({ configPath = null, noLoop = false } = {}) {
  const args = ["-m", "dots", "bridge"];
  if (configPath) args.push("-c", configPath);
  if (noLoop) args.push("--no-loop");
  return args;
}

class Backend extends EventEmitter {
  constructor({ root, configPath = null, noLoop = false }) {
    super();
    this.root = root;
    this.configPath = configPath;
    this.noLoop = noLoop;
    this.proc = null;
    this.pending = new Pending();
    this.stderrTail = [];
    this.ready = false;
  }

  start() {
    const { cmd, cwd } = resolveBackend({ root: this.root });
    const args = buildArgs({ configPath: this.configPath, noLoop: this.noLoop });
    this.emit("status", { state: "starting", cmd, args });

    this.proc = spawn(cmd, args, {
      cwd,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    // readline 按 \n 切并且会削掉 \r —— 协议现在保证是 \n,但宽进没坏处
    const rl = readline.createInterface({ input: this.proc.stdout });
    rl.on("line", (line) => this.#onLine(line));

    this.proc.stderr.on("data", (buf) => {
      const text = buf.toString("utf-8");
      this.stderrTail.push(text);
      if (this.stderrTail.length > 50) this.stderrTail.shift();
      this.emit("stderr", text);
    });

    this.proc.on("error", (err) => {
      this.emit("status", { state: "dead", reason: `${err.code ?? "错误"}: ${err.message}` });
      this.pending.rejectAll("后端起不来");
    });

    this.proc.on("exit", (code, signal) => {
      this.ready = false;
      // 所有还在等的请求一起 reject,否则界面永远转圈
      this.pending.rejectAll(`后端退出了(退出码 ${code ?? signal})`);
      this.emit("status", {
        state: "dead",
        code,
        signal,
        reason: this.stderrTail.join("").trim() || `退出码 ${code ?? signal}`,
      });
      this.emit("exit", { code, signal });
    });

    return this;
  }

  #onLine(line) {
    const msg = classify(line);
    if (!msg) return;
    if (msg.kind === "reply") {
      this.pending.settle(msg);
    } else if (msg.kind === "event") {
      if (msg.name === "ready") this.ready = true;
      this.emit("event", msg.name, msg.payload);
    } else {
      this.emit("junk", msg.error);
    }
  }

  /** 发一条命令,等它的应答。有副作用的探测一律别用这个(见 spec 约定 1)。 */
  send(cmd, fields = {}, timeoutMs = 15000) {
    const id = this.pending.nextId();
    const promise = this.pending.wait(id, timeoutMs);
    this.#write({ id, cmd, ...fields });
    return promise;
  }

  /** 发一条不等应答的(只在 quit 那种「发完就走」的场合用)。 */
  post(cmd, fields = {}) {
    this.#write({ cmd, ...fields });
  }

  #write(obj) {
    if (!this.proc || !this.proc.stdin.writable) return;
    this.proc.stdin.write(JSON.stringify(obj) + "\n");
  }

  /**
   * 优雅退出:发 quit,等它自己走;超时才 kill。
   * 多留 1 秒余量给「应答已经回来了但进程还在收尾」。
   */
  async stop() {
    if (!this.proc || this.proc.exitCode !== null) return;
    const dead = new Promise((resolve) => this.proc.once("exit", resolve));
    this.post("quit");
    const timer = new Promise((r) => setTimeout(r, EXIT_GRACE_MS));
    await Promise.race([dead, timer]);
    if (this.proc.exitCode === null) {
      this.emit("status", { state: "killing", reason: `${EXIT_GRACE_MS}ms 没退干净,强杀` });
      this.proc.kill();
    }
  }
}

module.exports = { Backend, resolveBackend, buildArgs, EXIT_GRACE_MS };
```

- [ ] **Step 4: 跑,确认通过**（`npm test` → 8 个 test 全过）

---

## Task 4: `main.js` + `preload.js` —— 窗口与 IPC

**Files:**
- Create: `desktop/electron/preload.js`
- Modify: `desktop/electron/main.js`

- [ ] **Step 1: preload.js（白名单，不暴露原始 ipcRenderer）**

```js
// preload.js —— 界面能碰到的全部东西就这些。
// 不暴露 ipcRenderer 本身:那是把整条 IPC 通道交出去。
const { contextBridge, ipcRenderer } = require("electron");

const EVENTS = [
  "ready", "state", "log", "chat_start", "chat", "ask",
  "output", "loop_stopped", "fatal",
];

contextBridge.exposeInMainWorld("dots", {
  /** 发一条命令,等应答。 */
  send: (cmd, fields) => ipcRenderer.invoke("dots:send", { cmd, ...fields }),
  /** 后端/进程状态:starting / ready / dead / killing */
  onStatus: (cb) => ipcRenderer.on("dots:status", (_e, payload) => cb(payload)),
  /** 协议事件(按名字订阅,省得界面自己 switch)。 */
  on: (name, cb) => {
    if (!EVENTS.includes(name)) return;
    ipcRenderer.on(`dots:event:${name}`, (_e, payload) => cb(payload));
  },
  onStderr: (cb) => ipcRenderer.on("dots:stderr", (_e, text) => cb(text)),
  onJunk: (cb) => ipcRenderer.on("dots:junk", (_e, text) => cb(text)),
});
```

- [ ] **Step 2: main.js 补全**

```js
// main.js —— 窗口 + 子进程的生命周期,以及把协议事件转给界面。
// 这里不做任何业务判断:后端说什么就转什么,界面自己决定怎么画。
const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("node:path");

const { Backend } = require("./backend");

const ROOT = path.join(__dirname, "..", "..");
let win = null;
let backend = null;

function createWindow() {
  win = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 720,
    minHeight: 520,
    backgroundColor: "#161A20",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.once("ready-to-show", () => win.show());
  win.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
  return win;
}

function startBackend() {
  backend = new Backend({ root: ROOT });
  backend.on("status", (s) => win?.webContents.send("dots:status", s));
  backend.on("stderr", (t) => win?.webContents.send("dots:stderr", t));
  backend.on("junk", (t) => win?.webContents.send("dots:junk", t));
  backend.on("event", (name, payload) => {
    win?.webContents.send(`dots:event:${name}`, payload);
  });
  backend.start();
}

ipcMain.handle("dots:send", async (_e, msg) => {
  if (!backend) throw new Error("后端还没起来");
  const { cmd, ...fields } = msg;
  return backend.send(cmd, fields);
});

app.whenReady().then(() => {
  createWindow();
  startBackend();
});

// 关窗口 = 退出(用户的决定)。先让后端优雅收尾,再退应用。
let quitting = false;
app.on("window-all-closed", async () => {
  if (quitting) return;
  quitting = true;
  try {
    await backend?.stop();
  } finally {
    app.quit();
  }
});
```

- [ ] **Step 3: 起一下看**

Run: `cd desktop && npm start`
Expected: 窗口起来。终端里能看到后端启动的 stderr（`python -m dots bridge` 会走到「没找到配置」或者 `ready`）。用任务管理器确认关窗口后没有残留 python。

（这一步会真的起 Python，需要机器上有能跑的 Python + 依赖。没配 dot.toml 的话后端会报「没找到配置」然后退出 —— 界面要能显示这句话，那是 Task 9 的事。）

---

## Task 5: 视觉地基 —— 变量 + 骨架

**方向已定（spec §5.4「值班台」）。这个任务只做变量和布局骨架，不碰业务。**

**Files:**
- Create: `desktop/renderer/theme.css`
- Create: `desktop/renderer/style.css`
- Modify: `desktop/renderer/index.html`

- [ ] **Step 1: theme.css —— 所有视觉参数，一个都不许写在别处**

```css
/* theme.css —— 主题就是这一组变量。
 * 组件只引用变量,不写死颜色 —— 以后「导入主题」= 读一份 CSS 覆盖 :root,
 * 加个按钮的事,不用回头改任何组件。本期不做导入 UI。
 *
 * 方向:值班台。底色是深石板蓝不是近黑;五个状态各占一个色相,
 * 靠颜色认状态而不是靠亮度;琥珀只留给「它停下来等你」。
 */
:root {
  --bg: #161A20;
  --panel: #1D222A;
  --panel-2: #222A34;
  --line: #262D38;
  --line-soft: #2A323D;

  --fg: #E4E7EB;
  --fg-dim: #A8B2C0;
  --fg-muted: #7C879A;

  /* 一屏里唯一的响:它停下来等你 */
  --wait: #E0A458;
  --wait-bg: #241F17;
  --wait-line: #4A3A1A;

  --st-ready: #5FA8D3;
  --st-running: #5FA8D3;
  --st-done: #7FA663;
  --st-interrupted: #E0A458;
  --st-failed: #C4634F;
  --st-pending: #8A7BA8;
  --st-review: #A08BB8;

  /* 中文优先:中文走系统黑体栈,西文和数字配随包的字体补个性 */
  --font: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
          "Noto Sans CJK SC", system-ui, sans-serif;
  /* 等宽只用在数字、编号、时间上 —— 不是整页等宽 */
  --font-num: "Cascadia Mono", Consolas, "SF Mono", monospace;

  --r: 4px;
  --gap: 10px;
}
```

- [ ] **Step 2: index.html 骨架（布局 B：上面监控，底部对话，最下面是命令抽屉）**

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:">
  <title>dots</title>
  <link rel="stylesheet" href="theme.css">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <!-- 顶栏:后端状态 + 循环启停 -->
  <header id="topbar">
    <b class="brand">dots</b>
    <span id="backend-state" class="pill">正在启动…</span>
    <span id="loop-slot"></span>
    <span class="spacer"></span>
    <button id="btn-doctor" class="btn ghost" title="逐项检查配置、密钥、模型、目录、数据库、事件源（dots doctor）">体检</button>
  </header>

  <!-- 待批横幅:只在有东西等你时出现 -->
  <section id="alert-bar" hidden></section>

  <!-- 监控区:目标 / 技能 / 活动 -->
  <main id="monitor">
    <section class="pane" id="pane-goals">
      <div class="pane-head"><h2>目标</h2><span class="pane-actions" id="goal-actions"></span></div>
      <div class="pane-body" id="goals"></div>
    </section>
    <section class="pane" id="pane-skills">
      <div class="pane-head"><h2>技能</h2><span class="pane-actions" id="skill-actions"></span></div>
      <div class="pane-body" id="skills"></div>
    </section>
    <section class="pane" id="pane-activity">
      <div class="pane-head"><h2>活动</h2><span class="pane-actions" id="activity-actions"></span></div>
      <div class="pane-body" id="activity"></div>
    </section>
  </main>

  <!-- 对话:常驻在最下面 -->
  <section id="chat">
    <div class="pane-head"><h2>对话</h2><span id="chat-status" class="hint"></span></div>
    <div id="chat-log" class="pane-body"></div>
    <form id="chat-form">
      <input id="chat-input" autocomplete="off" placeholder="说点什么…（回车发送）">
    </form>
  </section>

  <!-- 命令抽屉:默认收起,点把手展开;窗口矮了自动收起 -->
  <footer id="drawer" class="collapsed">
    <button id="drawer-handle" class="handle" aria-expanded="false">▴ 全部命令</button>
    <div id="drawer-body" class="drawer-body"></div>
  </footer>

  <!-- 命令输出:从底部升起的一层 -->
  <section id="output" hidden>
    <div class="pane-head">
      <h2 id="output-title">输出</h2>
      <span id="output-code" class="hint"></span>
      <span class="spacer"></span>
      <button id="output-copy" class="btn ghost">复制</button>
      <button id="output-close" class="btn ghost">关闭</button>
    </div>
    <pre id="output-body"></pre>
  </section>

  <!-- 聊天里的审批确认 -->
  <dialog id="ask-modal">
    <h3>它想干这个</h3>
    <p id="ask-action" class="ask-action"></p>
    <pre id="ask-args"></pre>
    <div class="modal-actions">
      <button id="ask-no" class="btn">拒绝</button>
      <button id="ask-yes" class="btn primary">批准</button>
    </div>
  </dialog>

  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 3: style.css —— 布局 B**

要点（完整 CSS 由实现者按变量写，这里给结构约定）：

- `body` 是 `display:flex; flex-direction:column; height:100vh`，四段：`#topbar`（flex:none）、`#alert-bar`（flex:none，`hidden` 时不存在）、`#monitor`（flex:1，可滚动）、`#chat`（flex:none，固定高度约 220px，可拖高）、`#drawer`（flex:none）。
- `#monitor` 里的三个 `.pane` 从上往下堆，`.pane-body` 各自滚动，`max-height` 按比例。
- `.pane-head` 用 `display:flex`，左侧 `h2` 小号大写感的标签，右侧 `.pane-actions` 放本面板自己的按钮。
- `#alert-bar`：`background: var(--wait-bg); border-left: 3px solid var(--wait)`，是**一屏里唯一的强调色**。
- 数字/编号/时间用 `font-family: var(--font-num)`；正文用 `var(--font)`。
- 状态色用 `[data-status="ready"] { color: var(--st-ready) }` 这类属性选择器，七个状态一个都不能少。

- [ ] **Step 4: 起一下看**

Run: `cd desktop && npm start`
Expected: 深色窗口，四段布局都在，抽屉收起，各面板标题和空态文案正确。**这一步不看数据，只看骨架。**

---

## Task 6: store 与渲染 —— 目标 / 待批 / 活动 / 技能

**Files:**
- Create: `desktop/renderer/app.js`

- [ ] **Step 1: store + 渲染骨架**

```js
// app.js —— store + 渲染。没有框架,没有构建步骤。
//
// 一条规矩:所有 DOM 写入都从 render() 走,别的事件处理只改 store。
// 这样「界面和后端不一致」只可能有一个来源。

const store = {
  status: { state: "starting" },
  goals: [],
  approvals: [],
  audit: [],
  skills: [],
  chat: [],          // {who:'you'|'dot', text, error?}
  chatBusy: false,
  output: null,      // {title, text, code}
  pending: new Map(),// 请求 id -> 说明(用来画「正在跑」)
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // 一律 textContent,不用 innerHTML
  return n;
};

/** 时间戳(秒,float)-> 「12s / 3m / 2h」 */
function ago(ts) {
  const d = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (d < 60) return `${d}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}
```

- [ ] **Step 2: 四个渲染函数**

```js
const STATUS_TEXT = {
  pending: "待批准", ready: "排队中", running: "在跑",
  interrupted: "停下等你", pending_review: "待复核", done: "完成", failed: "失败",
};

function renderGoals() {
  const box = $("goals");
  box.replaceChildren();
  if (!store.goals.length) {
    box.append(el("div", "empty", "还没有目标。「新建目标」给它派一个。"));
    return;
  }
  for (const g of store.goals) {
    const row = el("div", "row goal");
    row.dataset.status = g.status;
    row.append(el("code", "id", g.id));
    row.append(el("span", "status", STATUS_TEXT[g.status] ?? g.status));
    row.append(el("span", "text", g.text));
    if (g.summary) row.append(el("span", "summary", g.summary));
    if (g.evidence) row.append(el("span", "evidence", `证据 ${g.evidence}`));
    row.append(el("span", "num", `${g.tokens.toLocaleString()}`));
    row.append(el("span", "num", ago(g.updated_at)));
    const acts = el("span", "row-actions");
    acts.append(rowBtn("日志", `只看这个目标的活动记录（dots log --goal ${g.id}）`, () =>
      runCli(["log", "--goal", g.id], `日志 ${g.id}`)));
    row.append(acts);
    box.append(row);
  }
}
```

（`approvals` / `audit` / `skills` 同构，由实现者按同样形状补齐：`approvals` 每行是
`token` + 动作 + 等了多久 + **批准/拒绝两个行内按钮**；`audit` 每行是时间 + kind + detail；
`skills` 每行是名字 + 描述 + **看/删两个行内按钮**。

**注意 `approvals` 的字段名是 `tool` + `args(JSON 字符串)`，要自己 `JSON.parse`；
`ask` 事件里是 `action` + `args(对象)`。两套解析别写混** —— spec 里专门写了这条。）

- [ ] **Step 3: 接后端事件**

```js
window.dots.onStatus((s) => { store.status = s; render(); });

window.dots.on("state", (p) => {
  store.goals = p.goals ?? [];
  store.approvals = p.approvals ?? [];
  store.audit = p.audit ?? [];
  store.skills = p.skills ?? [];
  render();
});

window.dots.on("ready", (p) => {
  store.ready = p;
  store.status = { state: "ready", info: p };
  render();
});

window.dots.on("log", (p) => {
  // 流水是追加的,不进 store 的 audit(那是后端推的快照)
  appendActivity(p.level, p.text);
});

window.dots.on("fatal", (p) => {
  // 见 spec 约定 7:进程活着但某条线程死了,界面必须自己收场
  if (p.where === "chat") { store.chatBusy = false; pushChat("dot", `对话起不来:${p.error}`, true); }
  if (p.where === "tick") store.loopRunning = false;
  if (p.where === "cmd") finishOutputWithError(p.error);
  render();
});

window.dots.on("loop_stopped", () => { store.loopRunning = false; render(); });

function render() {
  renderTopbar();
  renderAlert();
  renderGoals();
  renderSkills();
  renderActivity();
  renderChat();
}
```

- [ ] **Step 4: 起一下看**

需要一份能跑的 `dot.toml`（在仓库根或 `~/.dots/`）。往 `workspace/inbox/` 扔个文件，看着目标列表自动多一行。

---

## Task 7: 对话 + 审批确认框

**Files:**
- Modify: `desktop/renderer/app.js`

- [ ] **Step 1: 发送与接收**

```js
$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const text = input.value.trim();
  if (!text || store.chatBusy) return;
  input.value = "";
  pushChat("you", text);
  store.chatBusy = true;
  renderChat();
  try {
    // 后端只回执「收下了」,真正的答案走 chat_start / chat 事件
    await window.dots.send("chat", { text });
  } catch (err) {
    pushChat("dot", `发不出去:${err.message}`, true);
    store.chatBusy = false;
    renderChat();
  }
});

window.dots.on("chat_start", () => { store.chatBusy = true; renderChat(); });
window.dots.on("chat", (p) => {
  store.chatBusy = false;
  pushChat("dot", p.text, Boolean(p.error));
  if (p.tokens) store.lastTokens = p.tokens;
  renderChat();
});
```

- [ ] **Step 2: 审批确认框**

```js
// 后端推 ask 之后会阻塞等我们回话 —— 聊天跑在它自己的线程上,
// 所以读 stdin 的那条线程还闲着,answer 送得进去。
window.dots.on("ask", (p) => {
  const dlg = $("ask-modal");
  $("ask-action").textContent = p.action ?? "?";
  $("ask-args").textContent = JSON.stringify(p.args ?? {}, null, 2);
  dlg.dataset.qid = p.qid;

  const answer = async (value) => {
    dlg.close();
    try {
      const r = await window.dots.send("answer", { qid: p.qid, value });
      // received:false = 这个 qid 已经过期(超过 600 秒),agent 那边早按拒绝处理了。
      // 界面必须如实说,不能因为请求发出去了就画成已批准。
      if (!r.received) pushChat("dot", "这个审批已经过期了,agent 早按拒绝处理了。", true);
    } catch (err) {
      pushChat("dot", `回话失败:${err.message}`, true);
    }
    renderChat();
  };

  $("ask-yes").onclick = () => answer(true);
  $("ask-no").onclick = () => answer(false);
  dlg.showModal();
});
```

- [ ] **Step 3: 手工验收**

起 bridge，在聊天里让它做一件会触发 `ask` 的事（比如「给 alice 发封邮件」）。确认：确认框弹出、点批准之后聊天继续、事件流里能看到 `resume={"approved":true}` 的效果。

---

## Task 8: 命令抽屉 —— 所有命令都变成按钮

**这是用户在 spec 评审阶段明确要求的：「所有命令必须变成按钮」「按钮显示中文」「悬浮显示它实际干什么」「抽屉可自动或手动显示隐藏」。**

**Files:**
- Modify: `desktop/renderer/app.js`
- Modify: `desktop/renderer/style.css`

- [ ] **Step 1: 命令表（16 条，中文标签 + tooltip + 参数形态）**

```js
// 按钮 = 一条 argv。复用后端那份命令表(cli._parser()),零重复:
// 以后 dots 加子命令,这里加一行就行,协议不用动。
//
// 有参数的命令点了先出小输入框;危险操作二次确认。
const COMMANDS = [
  // 配置
  { group: "配置", label: "生成配置", argv: ["init"],
    tip: "在当前目录生成 dot.toml（dots init）" },
  { group: "配置", label: "生成全局配置", argv: ["init", "--global"],
    tip: "写到 ~/.dots/，之后在任何目录敲 dots 都能用（dots init --global）",
    confirm: "会在 ~/.dots/ 写一份 dot.toml，确定？" },
  { group: "配置", label: "体检", argv: ["doctor"],
    tip: "逐项检查配置、密钥、模型、目录、数据库、事件源（dots doctor）" },
  { group: "配置", label: "自检", argv: ["selftest"],
    tip: "不联网、不调模型，跑一遍内置检查（dots selftest，约 30 秒）" },

  // 目标
  { group: "目标", label: "新建目标", native: "goal", input: "目标内容",
    tip: "交给后台慢慢跑" },
  { group: "目标", label: "跑一轮", argv: ["tick"], needsLoopOff: true,
    tip: "手动跑一轮就退，调试用（dots tick）。后台循环跑着的时候会被后端拦下" },
  { group: "目标", label: "全部目标", argv: ["ls"],
    tip: "列出目标，可按状态筛选（dots ls）" },
  { group: "目标", label: "活动日志", argv: ["log"],
    tip: "看它最近干了什么（dots log）" },

  // 审批（行内按钮用这两条）
  { group: "审批", label: "批准", argv: ["approve"], hidden: true,
    tip: "批准一个挂起的动作（dots approve <编号>）" },
  { group: "审批", label: "拒绝", argv: ["reject"], hidden: true,
    tip: "拒绝一个挂起的动作（dots reject <编号>）" },

  // 技能
  { group: "技能", label: "学习", argv: ["learn"], input: "要学什么（材料或一句描述）",
    tip: "把一段材料总结成技能（dots learn）" },
  { group: "技能", label: "技能库", argv: ["skills"],
    tip: "列出技能（dots skills）" },
  { group: "技能", label: "看技能", argv: ["skill", "show"], hidden: true, input: "技能名",
    tip: "看一份技能的正文（dots skill show <名>）" },
  { group: "技能", label: "删技能", argv: ["skill", "rm"], hidden: true, input: "技能名",
    tip: "删掉一份技能（dots skill rm <名>）", confirm: "会删掉 {arg} 这个技能的整个目录，确定？" },
  { group: "技能", label: "整理技能", argv: ["curate"],
    tip: "让 agent 合并重复、删掉没价值的（dots curate）",
    confirm: "它会真的删除/改写技能文件。先「技能库」看一眼现状？" },

  // 成本
  { group: "成本", label: "用量", argv: ["cost"], tip: "今日和累计 token 用量（dots cost）" },
];
```

- [ ] **Step 2: 画抽屉 + 行内按钮**

```js
function commandButton(spec, extraArg) {
  const btn = el("button", "btn");
  btn.textContent = spec.label;            // 中文标签,不显示 init/doctor 这种原名
  btn.title = spec.tip;                    // 悬浮说清它实际干什么,并带出对应的 dots 命令
  btn.onclick = () => invoke(spec, extraArg);
  return btn;
}

async function invoke(spec, extraArg) {
  let arg = extraArg;
  if (spec.input && arg == null) {
    arg = window.prompt(spec.input);
    if (arg == null || !arg.trim()) return;
    arg = arg.trim();
  }
  if (spec.confirm && !window.confirm(spec.confirm.replace("{arg}", arg ?? ""))) return;

  if (spec.native === "goal") {
    // 原生命令:返回 gid、写 origin=gui、先推快照再回应答。
    // 不走 CLI —— CLI 那条还会把「跑起来:python -m dots serve」打到输出面板里,在界面里很误导。
    try {
      await window.dots.send("goal", { text: arg });
    } catch (err) { showOutput(spec.label, `失败:${err.message}`, 1); }
    return;
  }
  runCli([...spec.argv, ...(arg ? [arg] : [])], spec.label);
}

/** 跑一条 CLI 命令,输出流式贴到底部那层输出面板。 */
async function runCli(argv, title) {
  showOutput(title, "正在跑…", null);
  try {
    const r = await window.dots.send("cli", { argv }, 180000);
    setOutput(r.out ?? "", r.code);
  } catch (err) {
    setOutput(`失败:${err.message}`, 1);
  }
}
```

`output` 事件的订阅（**必须「替换」而不是「追加」** —— 事件流和应答里的 `out` 是逐字节相等的同一份内容）：

```js
window.dots.on("output", (p) => {
  if (!store.output || store.output.reqId !== p.id) return;   // 不是当前这条
  appendOutput(p.chunk);                                      // 只追加 chunk
});
```

收尾以**应答**为准（`done:true` 那条事件只是标记，内容以应答里的 `out` 为准，直接整体替换一次，避免两处不一致）：

```js
function setOutput(text, code) {
  store.output.text = text;
  $("output-body").textContent = text;
  $("output-code").textContent = code == null ? "" : `退出码 ${code}`;
}
```

- [ ] **Step 3: 抽屉的自动/手动收起**

```js
// 手动:点把手展开/收起,状态记在本地,下次打开还是那样。
// 自动:窗口太矮时强制收起 —— 保证聊天区不被挤没。
const DRAWER_KEY = "dots.drawer.open";

function setDrawer(open) {
  const d = $("drawer");
  d.classList.toggle("collapsed", !open);
  $("drawer-handle").setAttribute("aria-expanded", String(open));
  $("drawer-handle").textContent = open ? "▾ 全部命令" : "▴ 全部命令";
  try { localStorage.setItem(DRAWER_KEY, open ? "1" : "0"); } catch {}
}

$("drawer-handle").onclick = () =>
  setDrawer($("drawer").classList.contains("collapsed"));

function autoDrawer() {
  const open = (() => { try { return localStorage.getItem(DRAWER_KEY) === "1"; } catch { return false; } })();
  setDrawer(open && window.innerHeight >= 640);
  $("drawer-handle").disabled = window.innerHeight < 640;   // 太矮就别让展开了
}
window.addEventListener("resize", autoDrawer);
autoDrawer();
```

- [ ] **Step 4: 行内按钮接上**

- 待批那一行的「批准 / 拒绝」→ `invoke(COMMANDS.find(c => c.argv[0] === "approve"), token)`
- 技能那一行的「看 / 删」→ 同理
- 「跑一轮」在 `store.loopRunning` 时置灰（`needsLoopOff`），title 补一句「后台正在跑，不用手动跑」

- [ ] **Step 5: 手工验收**

点「体检」→ 底部升起输出面板、流式出内容、结束显示退出码。
点「技能库」→ 有输出。
点「删技能」→ 弹确认 → 取消 → 什么都没发生。

---

## Task 9: 顶栏 —— 后端状态与 fatal 收场

**Files:**
- Modify: `desktop/renderer/app.js`

- [ ] **Step 1: 状态与循环启停**

**先做一条铁律：`ready` 之前所有输入置灰。** bridge 启动要先导入重依赖（本机 ~12 秒），
这期间它还没开始读 stdin —— 发过去的命令会堆在管道里，等 `ready` 那一刻一起处理，
先后会反直觉（实测：「跑一轮」和「开始值守」几乎同时发，后者抢到锁、前者被拒）。
用户点了没反应可以解释，点了然后顺序错乱解释不了。

```js
/** ready 之前一切输入都不许用。协议约定 8。 */
function setInputsEnabled(on) {
  for (const id of ["chat-input", "drawer-handle", "btn-doctor"]) {
    const n = $(id);
    if (n) n.disabled = !on;
  }
  document.body.dataset.ready = on ? "1" : "0";
}
```

`ready` 事件里带 `loop` 与 `tick_seconds`（spec §4），初始状态从那儿来，并且在这一刻才解禁：

```js
window.dots.on("ready", (p) => {
  store.ready = p;
  store.loopRunning = Boolean(p.loop);
  store.status = { state: "ready" };
  setInputsEnabled(true);
  render();
});
```

（`showOutput` / 输出面板的「关闭」「复制」不受影响 —— 它们不跟后端说话。
`ask-modal` 的按钮也不受影响 —— 那个必须能点，否则后端会一直阻塞等回话。
这条只钉「会让后端排队」的那几个入口。）

```js
function renderTopbar() {
  const s = store.status;
  const pill = $("backend-state");
  const TEXT = {
    starting: "正在启动后端…", ready: "值守中", dead: "后端已停", killing: "正在停止…",
  };
  pill.textContent = TEXT[s.state] ?? s.state;
  pill.dataset.state = s.state;

  const slot = $("loop-slot");
  slot.replaceChildren();
  if (s.state !== "ready") return;

  const tick = store.ready?.tick_seconds;
  const running = store.loopRunning;
  slot.append(el("span", "num", running ? `每 ${tick}s 一轮` : "循环已停"));
  const btn = el("button", "btn ghost", running ? "停止" : "开始值守");
  btn.title = running
    ? "停下 tick 循环。停下以 loop_stopped 事件为准,应答里的 running 可能还是 true"
    : "开始 tick 循环,它就会自己找活干了";
  btn.onclick = async () => {
    if (running) {
      const r = await window.dots.send("loop", { action: "stop" });
      // 回 false = 已经停了,不要再等事件(循环本来没跑时不会有 loop_stopped)
      if (!r.running) { store.loopRunning = false; }
      // 回 true = 还在一轮 tick 里,等 loop_stopped 事件来翻
    } else {
      const r = await window.dots.send("loop", { action: "start" });
      store.loopRunning = Boolean(r.started);
    }
    renderTopbar();
  };
  slot.append(btn);
}
```

`ready` 事件里带 `loop` 与 `tick_seconds`（spec §4），初始状态从那儿来：

```js
window.dots.on("ready", (p) => {
  store.ready = p;
  store.loopRunning = Boolean(p.loop);
  store.status = { state: "ready" };
  render();
});
```

- [ ] **Step 2: 后端起不来时给能照做的话**

```js
window.dots.onStatus((s) => {
  store.status = s;
  if (s.state === "dead") {
    // s.reason 是 stderr 的尾巴 —— 「没找到配置」「ModuleNotFoundError」都在里面
    showOutput("后端起不来", s.reason ?? "没有更多信息", null);
  }
  render();
});
```

**「没找到配置」那条要额外把「生成配置」按钮高亮出来** —— 那是用户下一步该做的事（spec §6）。

- [ ] **Step 3: 手工验收**

1. 有配置：起界面 → 顶栏从「正在启动后端…」变「值守中」+「每 60s 一轮」→ 点「停止」→ 变「循环已停」
2. 没配置：起界面 → 输出面板自动升起，里面是 `config.load` 那句「没找到配置。两种做法：…」

---

## Task 10: 端到端手工验收

**Files:** 无（只跑）

- [ ] **Step 1: 起界面**

```bash
cd desktop && npm start
```

- [ ] **Step 2: 逐条走**

1. 后端状态从「正在启动…」变「值守中」（约 2–9 秒，取决于环境）
2. 往 `workspace/inbox/` 扔个文件 → 活动面板出现新事件 → 目标列表自动多一行
3. 点「新建目标」→ 输入「把 inbox 整理成报告」→ 目标列表立刻多一行（`origin=gui`、状态 `ready`）
4. 聊天里问「工作区里现在有什么?」→ 有回答
5. 聊天里让它发邮件 → 弹确认框 → 点拒绝 → 聊天继续
6. 点「体检」→ 输出面板流式出内容 → 显示退出码 0
7. 点「技能库」「用量」「全部目标」→ 各有输出，中文不乱码
8. 点「整理技能」→ 弹二次确认 → 取消 → 什么都没发生
9. 点「停止」→ 顶栏变「循环已停」
10. 关窗口 → 任务管理器里**没有残留的 python.exe**

- [ ] **Step 3: 把结果记下来**

任何一条不过，记清是哪一条、现象、以及后端当时吐的原始 JSON 行（输出面板能复制）。

---

## 自检结果

**spec 覆盖检查：**

| spec 章节 | 落在哪个任务 |
|---|---|
| §2 架构（Electron 侧三个进程内文件） | Task 1/3/4 |
| §4 协议（含七条约定） | Task 2（分类/配对）、Task 3（客户端）、Task 6–9（各事件的界面反应） |
| §5.1 布局 B | Task 5 |
| §5.2 命令抽屉（中文标签 / tooltip / 16 条映射 / 危险二次确认 / 跑一轮置灰） | Task 8 |
| §5.3 输出面板 | Task 8 |
| §5.4 视觉方向 A（含七个状态的颜色） | Task 5（变量）、Task 6（属性选择器） |
| §5.5 主题机制（走 CSS 变量，不做导入 UI） | Task 5 Step 1 |
| §6 错误处理（后端起不来 / 配置缺失 / fatal 收场 / 关窗口 10 秒） | Task 3（10 秒）、Task 9（三种 fatal + dead） |
| §8 `resolveBackend()` 只在一个地方 | Task 3 Step 3 |

**不在本计划：** 打包（计划三）、聊天流式输出、主题导入 UI、命令超时/取消。

**已知的粗糙处（界面侧）：**
- 后端启动那 2–9 秒只能显示「正在启动后端…」（进程还没起来，拿不到进度）
- 聊天没有流式，慢模型下只有转圈
- `state` 是截断的（goals/audit 各 60 条），目标多了列表会静默少一行
