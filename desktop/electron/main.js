// main.js —— 窗口 + 子进程的生命周期,以及把协议事件转给界面。
// 这里不做任何业务判断:后端说什么就转什么,界面自己决定怎么画。
const { app, BrowserWindow, ipcMain } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const { Backend } = require("./backend");

/**
 * 后端在哪儿跑、用哪个 Python。
 *
 * 开发版(npm start):cwd = 仓库根 —— 那里有 dots 包、有 dot.toml,
 *                     Python 用 PATH 上那个。
 * 打包版:cwd = 用户数据目录(可写),Python 和依赖全用安装包自带的 runtime/,
 *         机器上没装过 Python 也能跑。配置从 ~/.dots/dot.toml 或这儿的
 *         ./dot.toml 找 —— 界面上那个「生成全局配置」按钮就是干这个的。
 *
 * DOTS_ROOT 是逃生口:打包版想指回某个检出(调试打包产物时用)。
 */
function backendPlan() {
  const resourcesPath = app.isPackaged ? process.resourcesPath : null;

  if (process.env.DOTS_ROOT) {
    return { root: process.env.DOTS_ROOT, resourcesPath };
  }
  if (!app.isPackaged) {
    return { root: path.join(__dirname, "..", ".."), resourcesPath: null };
  }

  const bundled = path.join(resourcesPath, "runtime", "python", "python.exe");
  if (!fs.existsSync(bundled)) {
    return {
      error:
        "安装包里没找到自带运行时:\n  " + bundled +
        "\n(打包时 extraResources 漏了 runtime/ 目录?)",
    };
  }
  return { root: app.getPath("userData"), resourcesPath };
}

let win = null;
let backend = null;

/**
 * 把一条消息发给渲染进程。
 *
 * 为什么要判 isDestroyed:退出流程里窗口已经没了、后端还在吐 status/stderr,
 * 那时 win 引用还在但 webContents 已经销毁,直接 send 会抛
 * 「Object has been destroyed」—— 正好炸在「关窗口」这条路上。
 */
function toRenderer(channel, payload) {
  if (!win || win.isDestroyed()) return;
  win.webContents.send(channel, payload);
}

/** 建窗口。先藏着,等首屏画好再露,免得闪一下白底。 */
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

/** 起后端进程,把它的四类消息原样转给界面。 */
function startBackend() {
  const plan = backendPlan();
  if (plan.error) {
    toRenderer("dots:status", { state: "dead", reason: plan.error });
    return;
  }

  backend = new Backend({ root: plan.root, resourcesPath: plan.resourcesPath });
  backend.on("status", (s) => toRenderer("dots:status", s));
  backend.on("stderr", (t) => toRenderer("dots:stderr", t));
  backend.on("junk", (t) => toRenderer("dots:junk", t));
  backend.on("event", (name, payload) => {
    toRenderer(`dots:event:${name}`, payload);
  });
  backend.start();
}

ipcMain.handle("dots:send", async (_e, msg) => {
  if (!backend) throw new Error("后端还没起来");
  // timeoutMs 由界面传:CLI 命令可能要几分钟(selftest / tick),
  // 15 秒的默认值只适合短命令。
  return backend.send(msg.cmd, msg.fields ?? {}, msg.timeoutMs ?? 15000);
});

app.whenReady().then(() => {
  createWindow();
  // 等页面加载完再起后端:preload 的订阅在那之前还没建立,
  // 早发的 status 会丢掉 —— 症状是界面永远停在「正在启动后端…」。
  win.webContents.once("did-finish-load", startBackend);
});

// 关窗口 = 退出(用户的决定)。先让后端优雅收尾,再退应用 ——
// 直接退会留下一个孤儿 python.exe,而且它没机会把 running 的目标复位。
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
