// preload.js —— 界面能碰到的全部东西就这些。
// 不暴露 ipcRenderer 本身:那是把整条 IPC 通道交出去。
const { contextBridge, ipcRenderer } = require("electron");

// 白名单。协议事件的名字只有这些(见 spec §4),界面订阅别的名字一律无效 ——
// 拼错的名字会在 preload 这一层就被挡下,而不是安静地永远收不到消息。
const EVENTS = [
  "ready", "state", "log", "chat_start", "chat", "ask",
  "output", "loop_stopped", "fatal",
];

contextBridge.exposeInMainWorld("dots", {
  /**
   * 发一条命令,等应答。收到 ready 之前不要调(见 protocol 约定 8)。
   *
   * timeoutMs 不传就是 15 秒。跑 CLI 命令要传大的 —— selftest 半分钟起步,
   * 而且命令线程是串行的,排在别人后面还得等。不传的话界面会在命令
   * 其实还活着的时候报「后端超时」。
   */
  send: (cmd, fields, timeoutMs) =>
    ipcRenderer.invoke("dots:send", { cmd, fields, timeoutMs }),
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
