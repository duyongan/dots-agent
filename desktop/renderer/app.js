/* app.js —— store + 渲染。没有框架,没有构建步骤。
 *
 * 一条规矩:所有 DOM 写入都从 render() 走,事件处理只改 store。
 * 这样「界面和后端不一致」只可能有一个来源。
 *
 * 界面纪律(都是协议里踩出来的,见 spec §4):
 *   - 收到 ready 之前一个命令都不许发(那之前后端还没开始读 stdin,
 *     命令会堆在管道里,等 ready 那一刻一起处理,先后会反直觉)
 *   - 每条命令都有应答,包括 chat;chat 的答案另外走 chat_start/chat 事件
 *   - 「停止」:应答回 running:false 就是已经停了,别再等事件
 *   - 审批在两条通道上字段名不一样:state 里是 tool + args(JSON 字符串),
 *     ask 事件里是 action + args(对象)
 */

// ─────────────────────────────────────────────── store

const store = {
  status: { state: "starting" }, // starting | ready | dead | killing
  ready: null, // ready 事件里的那一份(version/config/loop/tick_seconds…)
  loopRunning: false,
  goals: [],
  approvals: [],
  audit: [],
  skills: [],
  chat: [], // { who: "you" | "dot", text, err? }
  chatBusy: false,
  live: [], // log 事件追加的流水(和快照里的 audit 分开)
  output: null, // { title, text, code }
};

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text; // 一律 textContent,不用 innerHTML
  return n;
}

/** 秒(float)→ 「12s / 3m / 2h / 2d」 */
function ago(ts) {
  const d = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (d < 60) return `${d}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  if (d < 86400) return `${Math.floor(d / 3600)}h`;
  return `${Math.floor(d / 86400)}d`;
}

/** 时间戳 → 「15:04:07」 */
function clock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const STATUS_TEXT = {
  pending: "待批准",
  ready: "排队中",
  running: "在跑",
  interrupted: "停下等你",
  pending_review: "待复核",
  done: "完成",
  failed: "失败",
};

// ─────────────────────────────────────────── 通用小对话框

// window.prompt 在 Electron 里不支持(静默返回 null),window.confirm 的样子
// 也和这个界面不搭,所以两个都自己做。一个 dialog 兼两种模式:
// value 为 null 是「确认」,给了字符串是「输入」。
let _dlgResolve = null;

function askUser({ title, value = null, okText = "确定" }) {
  const dlg = $("input-modal");
  const field = $("input-field");
  $("input-title").textContent = title;
  $("input-ok").textContent = okText;
  field.hidden = value === null;
  field.value = value ?? "";
  return new Promise((resolve) => {
    _dlgResolve = resolve;
    dlg.showModal();
    if (value !== null) field.focus();
  });
}

function _settleDialog(result) {
  const dlg = $("input-modal");
  if (dlg.open) dlg.close();
  const r = _dlgResolve;
  _dlgResolve = null;
  if (r) r(result);
}

$("input-ok").onclick = () => {
  const field = $("input-field");
  if (field.hidden) return _settleDialog(true);
  const v = field.value.trim();
  if (!v) return; // 空的不算数,别关
  _settleDialog(v);
};
$("input-cancel").onclick = () => _settleDialog(null);
$("input-field").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    $("input-ok").click();
  }
});
// Esc 关掉对话框时也要把 promise 结掉,不然调用方永远挂着
$("input-modal").addEventListener("close", () => _settleDialog(null));

// ───────────────────────────────────────────── 命令表

// 按钮 = 一条 argv。复用后端那份命令表(cli._parser()),零重复:
// 以后 dots 加子命令,这里加一行就行,协议不用动。
//
// label 是中文 —— 界面上不出现 init/doctor 这种原始命令名;
// tip 在悬浮时说明它实际干什么,并带出对应的 dots 命令。
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
    tip: "不联网、不调模型，跑一遍内置检查。约 30 秒（dots selftest）" },

  // 目标
  { group: "目标", label: "新建目标", native: "goal", input: "要它做什么？",
    tip: "交给后台慢慢跑" },
  { group: "目标", label: "跑一轮", argv: ["tick"], needsLoopOff: true,
    tip: "手动跑一轮就退，调试用（dots tick）。后台循环跑着时会被后端拦下" },
  { group: "目标", label: "全部目标", argv: ["ls"],
    tip: "列出目标，可按状态筛选（dots ls）" },
  { group: "目标", label: "活动日志", argv: ["log"],
    tip: "看它最近干了什么（dots log）" },

  // 审批(行内按钮也用这两条)
  { group: "审批", label: "批准", argv: ["approve"], rowOnly: true,
    tip: "批准一个挂起的动作（dots approve <编号>）" },
  { group: "审批", label: "拒绝", argv: ["reject"], rowOnly: true,
    tip: "拒绝一个挂起的动作（dots reject <编号>）" },

  // 技能
  { group: "技能", label: "学习", argv: ["learn"], input: "要它学什么？（材料或一句描述）",
    tip: "把一段材料总结成技能，交给后台跑（dots learn）" },
  { group: "技能", label: "技能库", argv: ["skills"],
    tip: "列出技能（dots skills）" },
  { group: "技能", label: "看技能", argv: ["skill", "show"], rowOnly: true, input: "技能名",
    tip: "看一份技能的正文（dots skill show <名>）" },
  { group: "技能", label: "删技能", argv: ["skill", "rm"], rowOnly: true, input: "技能名",
    tip: "删掉一份技能（dots skill rm <名>）",
    confirm: "会删掉「{arg}」这个技能的整个目录，确定？" },
  { group: "技能", label: "整理技能", argv: ["curate"],
    tip: "让 agent 合并重复、删掉没价值的（dots curate）",
    confirm: "它会真的删除 / 改写技能文件。建议先用「技能库」看一眼现状。" },

  // 成本
  { group: "成本", label: "用量", argv: ["cost"],
    tip: "今日和累计 token 用量（dots cost）" },
];

function commandButton(spec, extraArg) {
  const btn = el("button", "btn", spec.label);
  btn.title = spec.tip;
  // 按钮能不能点是 render() 说了算,别在别处偷偷改 —— 否则
  // 「后端死了」和「循环在跑」两处状态会互相覆盖。
  if (store.status.state !== "ready") {
    btn.disabled = true;
    btn.title = "后端还没就绪（或者已经停了）";
  } else if (spec.needsLoopOff && store.loopRunning) {
    btn.disabled = true;
    btn.title = "后台循环正在跑，不用手动跑";
  }
  btn.onclick = () => invoke(spec, extraArg);
  return btn;
}

async function invoke(spec, extraArg) {
  let arg = extraArg;

  if (spec.input && arg == null) {
    arg = await askUser({ title: spec.input });
    if (arg == null || arg === false) return;
  }
  if (spec.confirm) {
    const msg = spec.confirm.replace("{arg}", arg ?? "");
    const ok = await askUser({ title: msg, okText: spec.confirm.includes("删掉") ? "删掉" : "确定" });
    if (ok !== true) return;
  }

  if (spec.native === "goal") {
    // 走原生命令:返回 gid、写 origin=gui、先推快照再回应答。
    // 不走 CLI —— CLI 那条还会把「跑起来:python -m dots serve」打进输出面板,
    // 在界面里很误导。
    try {
      await window.dots.send("goal", { text: arg });
    } catch (err) {
      showOutput(spec.label, `失败：${err.message}`, 1);
    }
    return;
  }

  runCli([...spec.argv, ...(arg ? [arg] : [])], spec.label);
}

// ─────────────────────────────────────────── 命令输出面板

/** 跑一条 CLI 命令,输出流式贴到底部那层输出面板。 */
async function runCli(argv, title) {
  showOutput(title, "正在跑…", null);
  try {
    const r = await window.dots.send("cli", { argv }, 180000);
    // 收尾以应答里的 out 为准,不用 done:true 那条事件 ——
    // 两者内容逐字节相等,两处都用会不一致。
    setOutput(r.out ?? "", r.code);
  } catch (err) {
    setOutput(`失败：${err.message}`, 1);
  }
}

function showOutput(title, text, code) {
  store.output = { title, text, code, final: false };
  $("output").hidden = false;
  $("output-title").textContent = title;
  $("output-body").textContent = text;
  setOutputCode(code);
}

function appendOutput(chunk) {
  // 定稿之后一个字都不再收:应答里的 out 才是权威。
  //
  // 为什么需要这个标记:ipcMain.handle 的应答和 webContents.send 的事件
  // 是两条通道,谁先到不保证 —— 实测「体检」的输出尾部会重复一遍,
  // 就是应答先到、几个迟到的 output 事件又追加了一次。
  if (!store.output || store.output.final || !chunk) return;
  store.output.text += chunk;
  const body = $("output-body");
  const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  body.textContent = store.output.text;
  if (stick) body.scrollTop = body.scrollHeight;
}

function setOutput(text, code) {
  if (!store.output) store.output = { title: "输出" };
  store.output.text = text;
  store.output.code = code;
  store.output.final = true;
  $("output-body").textContent = text;
  setOutputCode(code);
  $("output-body").scrollTop = $("output-body").scrollHeight;
}

function setOutputCode(code) {
  const n = $("output-code");
  n.textContent = code == null ? "" : `退出码 ${code}`;
  n.dataset.bad = code ? "1" : "0";
}

$("output-close").onclick = () => {
  $("output").hidden = true;
  store.output = null;
};

$("output-copy").onclick = async () => {
  try {
    await navigator.clipboard.writeText(store.output?.text ?? "");
    $("output-copy").textContent = "已复制";
    setTimeout(() => ($("output-copy").textContent = "复制"), 1200);
  } catch {
    $("output-copy").textContent = "复制失败";
    setTimeout(() => ($("output-copy").textContent = "复制"), 1200);
  }
};

// ───────────────────────────────────────────── 渲染

function renderTopbar() {
  const s = store.status;
  const pill = $("backend-state");
  const TEXT = {
    starting: "正在启动后端…",
    ready: "值守中",
    dead: "后端已停",
    killing: "正在停止…",
  };
  pill.textContent = TEXT[s.state] ?? s.state;
  pill.dataset.state = s.state;

  const slot = $("loop-slot");
  slot.replaceChildren();
  if (s.state !== "ready") return;

  const tick = store.ready?.tick_seconds;
  slot.append(el("span", "hint", store.loopRunning ? `每 ${tick}s 一轮` : "循环已停"));

  const btn = el("button", "btn ghost", store.loopRunning ? "停止" : "开始值守");
  btn.title = store.loopRunning
    ? "停下 tick 循环。停下以 loop_stopped 事件为准，应答里的 running 可能还是 true"
    : "开始 tick 循环，它就会自己找活干了";
  btn.onclick = async () => {
    btn.disabled = true;
    try {
      if (store.loopRunning) {
        const r = await window.dots.send("loop", { action: "stop" });
        // 回 running:false = 已经停了,不要再等事件
        // (循环本来没跑时不会有 loop_stopped)。回 true = 还在一轮 tick 里,等事件。
        if (!r.running) store.loopRunning = false;
      } else {
        const r = await window.dots.send("loop", { action: "start" });
        store.loopRunning = Boolean(r.started);
      }
    } catch (err) {
      showOutput("循环启停", `失败：${err.message}`, 1);
    }
    render();
  };
  slot.append(btn);
}

function renderAlert() {
  const bar = $("alert-bar");
  bar.replaceChildren();
  if (!store.approvals.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  const first = store.approvals[0];
  const more = store.approvals.length > 1 ? `（还有 ${store.approvals.length - 1} 个）` : "";
  bar.append(el("span", null, `⚠ ${summarizeApproval(first)}${more}`));

  const approve = COMMANDS.find((c) => c.argv[0] === "approve");
  const reject = COMMANDS.find((c) => c.argv[0] === "reject");
  for (const a of store.approvals) {
    const wrap = el("span", "row-actions");
    wrap.style.opacity = "1";
    const ap = el("button", "btn primary", "批准");
    ap.title = `${a.tool}：${a.args}（dots approve ${a.token}）`;
    ap.onclick = () => invoke(approve, a.token);
    const rj = el("button", "btn", "拒绝");
    rj.title = `不执行这个动作（dots reject ${a.token}）`;
    rj.onclick = () => invoke(reject, a.token);
    wrap.append(el("code", "id", a.token), ap, rj);
    bar.append(wrap);
    if (store.approvals.length > 3) break;
  }
}

/** 待批动作的人话摘要。注意 state 里是 tool + args(JSON 字符串)。 */
function summarizeApproval(a) {
  let args = {};
  try {
    args = JSON.parse(a.args || "{}");
  } catch {
    /* 坏了就只显示工具名 */
  }
  if (a.tool === "send_email") {
    return `${a.token} 想发邮件给 ${args.to ?? "?"}：「${String(args.subject ?? "").slice(0, 40)}」`;
  }
  if (a.tool === "run_shell") {
    return `${a.token} 想执行命令：${String(args.cmd ?? "").slice(0, 60)}`;
  }
  return `${a.token} 想执行 ${a.tool}`;
}

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
    row.append(el("span", "num", Number(g.tokens ?? 0).toLocaleString()));
    row.append(el("span", "num", ago(g.updated_at)));

    const acts = el("span", "row-actions");
    const log = el("button", "btn tiny", "日志");
    log.title = `只看这个目标的活动记录（dots log --goal ${g.id}）`;
    log.onclick = () => runCli(["log", "--goal", g.id], `日志 ${g.id}`);
    acts.append(log);
    row.append(acts);
    box.append(row);
  }
}

function renderSkills() {
  const box = $("skills");
  box.replaceChildren();
  if (!store.skills.length) {
    box.append(el("div", "empty", "技能库是空的。它干活时会自己攒，「学习」也能喂材料。"));
    return;
  }
  for (const s of store.skills) {
    const row = el("div", "row");
    row.append(el("code", "id", s.name));
    row.append(el("span", "text", s.description));
    const acts = el("span", "row-actions");
    const show = COMMANDS.find((c) => c.label === "看技能");
    const rm = COMMANDS.find((c) => c.label === "删技能");
    const b1 = el("button", "btn tiny", "看");
    b1.title = `看 ${s.name} 的正文（dots skill show ${s.name}）`;
    b1.onclick = () => invoke(show, s.name);
    const b2 = el("button", "btn tiny", "删");
    b2.title = `删掉 ${s.name}（dots skill rm ${s.name}）`;
    b2.onclick = () => invoke(rm, s.name);
    acts.append(b1, b2);
    row.append(acts);
    box.append(row);
  }
}

function renderActivity() {
  const box = $("activity");
  box.replaceChildren();
  const rows = [
    ...store.audit.map((a) => ({ ts: a.ts, kind: a.kind, detail: a.detail })),
    ...store.live,
  ].sort((a, b) => a.ts - b.ts);

  if (!rows.length) {
    box.append(el("div", "empty", "还没有活动。它跑起来之后这里会自己长东西。"));
    return;
  }
  for (const r of rows.slice(-120)) {
    const row = el("div", "row");
    row.append(el("span", "num", clock(r.ts)));
    row.append(el("span", "num", r.kind));
    row.append(el("span", "text", r.detail ?? ""));
    box.append(row);
  }
  box.scrollTop = box.scrollHeight;
}

function renderChat() {
  const box = $("chat-log");
  box.replaceChildren();
  for (const m of store.chat) {
    const line = el("div", `line ${m.who === "you" ? "you" : "dot"}${m.err ? " err" : ""}`);
    line.append(el("span", "who", m.who === "you" ? "你" : "dot"));
    line.append(el("span", "what", m.text));
    box.append(line);
  }
  if (store.chatBusy) {
    const line = el("div", "line dot");
    line.append(el("span", "who", "dot"));
    line.append(el("span", "what", "正在想…"));
    box.append(line);
  }
  box.scrollTop = box.scrollHeight;
  $("chat-status").textContent = store.chatBusy ? "" : "";
}

function pushChat(who, text, err = false) {
  store.chat.push({ who, text, err });
  if (store.chat.length > 300) store.chat.shift();
}

function render() {
  renderTopbar();
  renderAlert();
  renderGoals();
  renderSkills();
  renderActivity();
  renderChat();
  renderActions();
  // 抽屉也要重画:里面「跑一轮」的可用状态跟着循环走
  renderDrawer();
  autoDrawer();
}

/** 面板标题右边那些按钮:每个面板管自己的 */
function renderActions() {
  const put = (id, specs) => {
    const box = $(id);
    box.replaceChildren();
    for (const s of specs) box.append(commandButton(s));
  };
  const byLabel = (l) => COMMANDS.find((c) => c.label === l);
  put("goal-actions", [byLabel("新建目标"), byLabel("跑一轮"), byLabel("全部目标"), byLabel("活动日志")]);
  put("skill-actions", [byLabel("学习"), byLabel("技能库"), byLabel("整理技能")]);
  put("activity-actions", [byLabel("用量"), byLabel("自检")]);
}

// ─────────────────────────────────────────── 命令抽屉

const DRAWER_KEY = "dots.drawer.open";

function setDrawer(open) {
  $("drawer").classList.toggle("collapsed", !open);
  $("drawer-handle").setAttribute("aria-expanded", String(open));
  $("drawer-handle").textContent = open ? "▾ 全部命令" : "▴ 全部命令";
  try {
    localStorage.setItem(DRAWER_KEY, open ? "1" : "0");
  } catch {
    /* 隐私模式下写不了,无所谓 */
  }
}

$("drawer-handle").onclick = () =>
  setDrawer($("drawer").classList.contains("collapsed"));

function renderDrawer() {
  const box = $("drawer-body");
  box.replaceChildren();
  let lastGroup = null;
  for (const spec of COMMANDS) {
    if (spec.rowOnly) continue; // 这两条只在待批那一行用
    if (spec.group !== lastGroup) {
      if (lastGroup) box.append(el("span", "sep"));
      box.append(el("span", "grp", spec.group));
      lastGroup = spec.group;
    }
    box.append(commandButton(spec));
  }
}

function autoDrawer() {
  const short = window.innerHeight < 640;
  $("drawer-handle").disabled = short || store.status.state !== "ready";
  if (short) {
    $("drawer").classList.add("collapsed");
    return;
  }
  let open = false;
  try {
    open = localStorage.getItem(DRAWER_KEY) === "1";
  } catch {
    /* 读不到就当收起 */
  }
  setDrawer(open);
}

window.addEventListener("resize", autoDrawer);

// ─────────────────────────────── ready 之前一律不许发命令

// bridge 启动要先导入重依赖(本机 ~12 秒),那之前它还没开始读 stdin ——
// 发过去的命令会堆在管道里,等 ready 那一刻一起处理,先后会反直觉。
// 用户点了没反应可以解释,点了然后顺序错乱解释不了。
function setInputsEnabled(on) {
  document.body.dataset.ready = on ? "1" : "0";
  const chat = $("chat-input");
  if (chat) chat.disabled = !on;
  // 顶栏那个「体检」是 index.html 里的静态按钮,render() 不会重建它,
  // 只能在这儿单独管。
  const doc = $("btn-doctor");
  if (doc) doc.disabled = !on;
  // 抽屉和面板上的按钮不在这里逐个关 —— 它们的可用状态由 commandButton
  // 按 store.status 算,render() 一画就对了。这里只负责触发重画。
  render();
}

// ─────────────────────────────────────────── 接后端

window.dots.onStatus((s) => {
  store.status = s;
  if (s.state === "dead") {
    // s.reason 是 stderr 的尾巴 ——「没找到配置」「ModuleNotFoundError」都在里面
    store.loopRunning = false;
    setInputsEnabled(false);
    showOutput("后端起不来", s.reason ?? "没有更多信息", null);
  }
  render();
});

window.dots.onStderr(() => {
  /* stderr 的尾巴已经在 status.reason 里了,这里不重复画 */
});

window.dots.onJunk((text) => {
  store.live.push({ ts: Date.now() / 1000, kind: "协议", detail: text });
  renderActivity();
});

window.dots.on("ready", (p) => {
  store.ready = p;
  store.loopRunning = Boolean(p.loop);
  store.status = { state: "ready" };
  setInputsEnabled(true);
  render();
});

window.dots.on("state", (p) => {
  store.goals = p.goals ?? [];
  store.approvals = p.approvals ?? [];
  store.audit = p.audit ?? [];
  store.skills = p.skills ?? [];
  render();
});

window.dots.on("log", (p) => {
  store.live.push({ ts: Date.now() / 1000, kind: p.level ?? "info", detail: p.text ?? "" });
  if (store.live.length > 300) store.live.shift();
  renderActivity();
});

window.dots.on("loop_stopped", (p) => {
  store.loopRunning = false;
  store.live.push({
    ts: Date.now() / 1000,
    kind: "loop",
    detail: p.reason === "crashed" ? "循环自己崩了" : "循环已停",
  });
  render();
});

window.dots.on("fatal", (p) => {
  // 进程活着但某条线程死了,界面必须自己收场(见 spec 约定 7)
  store.live.push({ ts: Date.now() / 1000, kind: "fatal", detail: `${p.where}: ${p.error}` });
  if (p.where === "chat") {
    store.chatBusy = false;
    pushChat("dot", `对话起不来：${p.error}`, true);
  }
  if (p.where === "tick") {
    store.loopRunning = false;
  }
  if (p.where === "cmd") {
    setOutput(`命令线程出错：\n${p.error}`, 1);
  }
  render();
});

window.dots.on("output", (p) => {
  // 命令线程是串行的,同一时刻只有一条命令在跑,所以 live chunk 归当前面板。
  // 就算归错了也会被应答里的 out 整体覆盖掉 —— 那才是权威。
  if (!store.output) return;
  if (p.done) return; // 收尾以应答为准
  appendOutput(p.chunk ?? "");
});

// ───────────────────────────────────────────── 对话

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
    // 后端只回执「收下了」,真正的答案走 chat_start / chat 事件(带 id)
    await window.dots.send("chat", { text });
  } catch (err) {
    pushChat("dot", `发不出去：${err.message}`, true);
    store.chatBusy = false;
    renderChat();
  }
});

window.dots.on("chat_start", () => {
  store.chatBusy = true;
  renderChat();
});

window.dots.on("chat", (p) => {
  store.chatBusy = false;
  pushChat("dot", p.text, Boolean(p.error));
  renderChat();
});

// ─────────────────────────────────────────── 审批确认

// 后端推 ask 之后会阻塞等我们回话 —— 聊天跑在它自己的线程上,
// 所以读 stdin 的那条线程还闲着,answer 送得进去。
window.dots.on("ask", (p) => {
  const dlg = $("ask-modal");
  $("ask-action").textContent = p.action ?? "?";
  // ask 事件里是 action + args(已经是对象),和 state 里那套不一样
  $("ask-args").textContent = JSON.stringify(p.args ?? {}, null, 2);

  const answer = async (value) => {
    dlg.close();
    try {
      const r = await window.dots.send("answer", { qid: p.qid, value });
      // received:false = 这个 qid 已经过期(超过 600 秒),
      // agent 那边早按拒绝处理了。不能因为请求发出去了就画成已批准。
      if (!r.received) {
        pushChat("dot", "这个审批已经过期了，agent 早按拒绝处理了。", true);
      }
    } catch (err) {
      pushChat("dot", `回话失败：${err.message}`, true);
    }
    renderChat();
  };

  $("ask-yes").onclick = () => answer(true);
  $("ask-no").onclick = () => answer(false);
  dlg.showModal();
});

// ─────────────────────────────────────────────── 起步

$("btn-doctor").onclick = () =>
  runCli(["doctor"], "体检");

renderDrawer();
setInputsEnabled(false);
render();
