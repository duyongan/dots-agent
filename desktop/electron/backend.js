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

/**
 * 用哪个 python、带什么参数。抽成纯函数是为了能单测。
 *
 * 顺序(开发时怎么用都不奇怪,打包后必须自给自足):
 *   1. DOTS_PYTHON —— 调试用,指哪用哪
 *   2. 安装包自带的运行时 —— 打包版走这条,机器上没装 Python 也能跑
 *   3. 项目里的 venv —— 开发时想隔离就用它
 *   4. PATH 上的 python —— 开发时的默认
 */
function resolveBackend({ root, env = process.env, exists = fs.existsSync,
                          resourcesPath = null } = {}) {
  if (env.DOTS_PYTHON) return { cmd: env.DOTS_PYTHON, cwd: root };
  if (resourcesPath) {
    const bundled = path.join(resourcesPath, "runtime", "python", "python.exe");
    if (exists(bundled)) return { cmd: bundled, cwd: root };
  }
  const venv = path.join(root, "desktop", ".venv", "Scripts", "python.exe");
  if (exists(venv)) return { cmd: venv, cwd: root };
  return { cmd: "python", cwd: root };
}

/** 拼 `python -m dots bridge` 的参数。循环默认开,`--no-loop` 才关。 */
function buildArgs({ configPath = null, noLoop = false } = {}) {
  const args = ["-m", "dots", "bridge"];
  if (configPath) args.push("-c", configPath);
  if (noLoop) args.push("--no-loop");
  return args;
}

class Backend extends EventEmitter {
  constructor({ root, configPath = null, noLoop = false, resourcesPath = null }) {
    super();
    this.root = root;
    this.configPath = configPath;
    this.noLoop = noLoop;
    this.resourcesPath = resourcesPath;
    this.proc = null;
    this.pending = new Pending();
    this.stderrTail = [];
    this.ready = false;
    this.exited = false;
  }

  start() {
    const { cmd, cwd } = resolveBackend({
      root: this.root,
      resourcesPath: this.resourcesPath,
    });
    const args = buildArgs({ configPath: this.configPath, noLoop: this.noLoop });
    this.exited = false;
    this.emit("status", { state: "starting", cmd, args });

    this.proc = spawn(cmd, args, {
      cwd,
      // PYTHONUNBUFFERED 是必须的:不设的话 stdout 会攒够一块才吐,
      // 协议行会延迟几秒才到 —— 界面看起来就是「点了没反应」。
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    // readline 按 \n 切并且会削掉 \r —— 协议现在保证是 \n,但宽进没坏处
    const rl = readline.createInterface({ input: this.proc.stdout });
    rl.on("line", (line) => this.#onLine(line));

    // 子进程咽气的瞬间我们可能正好在写,write 会 EPIPE。不接住的话
    // Node 把流上的 'error' 抛成主进程的未捕获异常 —— 整个界面崩掉,
    // 而实际发生的事只是「后端没了」,那个由 exit 处理器如实汇报。
    this.proc.stdin.on("error", () => {});

    this.proc.stderr.on("data", (buf) => {
      const text = buf.toString("utf-8");
      this.stderrTail.push(text);
      if (this.stderrTail.length > 50) this.stderrTail.shift();
      this.emit("stderr", text);
    });

    this.proc.on("error", (err) => {
      // 起不来的进程不会有 exit 事件(spawn 失败只发 error/close),所以在这里
      // 就得把 exited 立起来 —— 否则关窗口时 stop() 会白等满 10 秒才肯退。
      this.exited = true;
      this.emit("status", { state: "dead", reason: `${err.code ?? "错误"}: ${err.message}` });
      this.pending.rejectAll("后端起不来");
    });

    this.proc.on("exit", (code, signal) => {
      this.ready = false;
      this.exited = true;
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

  /**
   * 发一条命令,等它的应答。
   *
   * **收到 ready 之前不要调它** —— 那之前后端还没开始读 stdin,命令会堆在
   * 管道缓冲区里,等 ready 那一刻一起按坏顺序处理(spec 约定 8)。
   *
   * 后端已经死了就直接拒 —— 不能让它挂到超时才报错:命令通道的超时是 180 秒,
   * 用户点一下按钮转三分钟才看到「失败」,比当场说「后端没了」糟得多。
   */
  send(cmd, fields = {}, timeoutMs = 15000) {
    if (!this.proc || this.exited) {
      return Promise.reject(new Error("后端已经没了,命令没发出去"));
    }
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
   *
   * 用 exited 标记而不是 `exitCode !== null` 判断「已经死了」:被信号杀掉的
   * 进程 exitCode 是 null,用 exitCode 判断会误以为它还活着,白等满 10 秒
   * 才走完退出流程。
   */
  async stop() {
    if (!this.proc || this.exited) return;
    const dead = new Promise((resolve) => this.proc.once("exit", resolve));
    this.post("quit");
    const timer = new Promise((r) => setTimeout(r, EXIT_GRACE_MS));
    await Promise.race([dead, timer]);
    if (!this.exited) {
      this.emit("status", { state: "killing", reason: `${EXIT_GRACE_MS}ms 没退干净,强杀` });
      this.proc.kill();
    }
  }
}

module.exports = { Backend, resolveBackend, buildArgs, EXIT_GRACE_MS };
