# dots 桌面界面（Electron）设计文档

日期：2026-10-16
状态：待评审

## 1. 要解决什么

现在用 `dots` 得开终端：`dots serve` 常驻、`dots goal` 下目标、`dots approve` 批审批、裸敲 `dots` 进交互终端聊天。四件事散在四个入口，而且得记住命令。

要一个 Windows 桌面程序：**一个窗口装下全部四个入口，每一条命令都有一个按钮**。

### 目标

- 双击安装包装完就能用，不需要先开终端
- 四条主线全在窗口里：看它在干什么 / 下目标和批审批 / 和它对话 / 控制后台跑不跑
- CLI 子命令**全部有按钮**（`serve` 除外 —— 常驻循环由界面自己管，不是一条能点的命令），不要求用户背命令
- 界面是自己看着舒服、能长期开着的东西，不是又一个深色仪表盘

### 明确不做（本期）

| 不做 | 原因 |
|---|---|
| **主题导入 UI** | 结构留出来（所有视觉参数走 CSS 变量），加导入是以后加一个按钮的事 |
| **自带 Python 运行时** | 先做开发版（调本机 Python），架构上把「怎么找到 Python」收在一个函数里，以后替换 |
| **托盘常驻** | 已定：关窗口 = 退出 |
| **流式输出 token** | 聊天一次性返回；先给「正在想…」状态，流式留到下一版 |
| **自动更新、多窗口、i18n** | 用不上 |

### 一个先量后定的数字

`from dots import cli` 在干净环境 2.2 秒，在开发者本机（conda base 里装了 `transformers`，langchain 会顺手 `import torch`）**8.7 秒**。

结论：**界面不能靠反复调命令行**（点一下卡 8 秒），Python 侧必须长驻一个进程，把建图成本只付一次。这条数字直接决定了下面整个架构。

## 2. 架构

```
┌─ Electron ────────────────────────────────────────┐
│  main.js        窗口 + 生命周期 + IPC 转发          │
│  backend.js     子进程管理 + JSON-lines 协议       │
│  preload.js     contextBridge 白名单              │
│  renderer/      界面（原生 HTML/CSS/JS，无构建）    │
└──────────────┬────────────────────────────────────┘
               │ spawn: python -m dots bridge
               │ stdin/stdout 一行一个 JSON
┌──────────────▼────────────────────────────────────┐
│  dots/bridge.py    协议胶水（不含业务逻辑）          │
│    ├ reader 线程(主)  独占读 stdin，分发             │
│    ├ tick   线程      worker.tick() 循环            │
│    ├ chat   线程      worker.chat_send()           │
│    └ command 线程     cli.main(argv)，串行执行       │
└──────────────┬────────────────────────────────────┘
               │ 复用（不重写）
               ▼
   worker / db / approvals / skills / rules / config
```

### 为什么是 stdio 而不是本地 HTTP

- **零端口、零鉴权面**。本地 HTTP 服务没有鉴权的话，浏览器里任何一个网页都能 POST 到 `127.0.0.1` 让它发邮件或跑命令（CSRF / DNS rebinding）。要补 token、CORS、端口冲突，工作量反而更大。
- **子进程活不过窗口**，正好等于已定的「关窗口 = 退出」，不用额外做单实例和幽灵进程清理。
- 和项目既有的「没有 socket，没有 IPC；CLI 只往 SQLite 写，常驻进程下一轮自己看见」是同一个味道。

代价：没有 curl 那样的调试手感（用 `DOTS_BRIDGE_DEBUG=1` 把协议原文打到日志文件补上），以后想让浏览器/手机也连得重做（本期不需要）。

### 为什么四条线程

| 线程 | 职责 | 不这样会怎样 |
|---|---|---|
| **reader**（主） | 独占读 stdin 并分发 | 聊天中途要弹审批确认时必须还有人能读 stdin，否则死锁 |
| **tick** | tick 循环 | 一个目标可能跑几分钟，会挡住聊天 |
| **chat** | 聊天 | 用户打字不该等 tick |
| **command** | 跑 `cli.main(argv)` | `selftest` 要 30 秒；放 reader 上会把整个界面卡住 |

**每个线程各有自己的 `RunCtx`、sqlite 连接；tick 和 chat 还各有自己的 checkpointer。**

tick 和 chat 共用一份 checkpointer 不破坏交接：交接用的 thread_id 是目标 id，只有 tick 线程跑目标；聊天固定用 `"chat"` 这个 thread_id。两条线永不重叠。这也延续了项目里已有的规则——`worker.checkpointer()` 的注释写着 checkpointer 必须自己一条连接，不能和应用库共用，否则互相 commit 掉对方的事务。

## 3. Python 侧改动

### 3.1 `graph.py`：`CURRENT` 从模块全局改成线程局部

现状是模块级单值，代码里自己标了：

```python
# ponytail: 全局单值;哪天要并发跑多个 goal,改成从 config 里取。
CURRENT: RunCtx | None = None
```

现在到了改它的时候。改成：

```python
_local = threading.local()

def current() -> RunCtx | None: ...
def set_current(ctx: RunCtx | None) -> None: ...
```

并替换约 8 处调用点（`worker.py` 5 处、测试 3 处）。**注意不能用模块级 `__getattr__` 糊过去**：读走 `__getattr__`，写是直接设模块属性，写上之后读就再也走不到 `__getattr__` 了，线程局部语义会静默失效。

### 3.2 `bridge.py`：新增，协议胶水

结构上把协议处理做成**纯函数**，外面套一层薄的 `main()`：

```python
def handle(cmd: dict, session: Session) -> dict | None:
    """处理一条命令。返回应答 dict，或 None（表示异步/推送型）。"""

def main(argv=None) -> int:
    """读 stdin 行 -> handle -> 写 stdout。"""
```

这样测试能直接调 `handle`，不用起真进程 —— 和 `rules.py` / `skills.py` 那种「纯逻辑核心 + `demo()`」的既有写法一致。

**全部复用，不重写业务逻辑**：`worker.build_agents` / `worker.chat_agents` / `worker.tick` / `worker.summarize` / `worker.recover_stale_runs` / `db.*` / `approvals.*`。

**退出时把 running 的目标放回 ready**：`recover_stale_runs` 的阈值是 1800 秒，正常关窗口不该让目标白等半小时。bridge 收到 `quit` 先复位再退。

### 3.3 `cli.py`：加 `bridge` 子命令 + 重依赖改惰性导入

`cli.py` 现在在模块顶部 `from dots import worker`，于是 `dots ls`（只是读 SQLite 里几行）也要付完整的 deepagents 导入成本。改成用到时才导入，`ls` / `log` / `cost` / `approve` / `reject` 从 8.7 秒降到 0.3 秒级。

这不只是为了界面：命令行下敲 `dots ls` 等 8 秒本来就是浪费。

### 3.4 通用命令通道：命令按钮的实现方式

**bridge 不逐个实现这些命令**，而是复用 CLI 自己那一份命令表：

```
Node → {"id":7,"cmd":"cli","argv":["doctor"]}
```

bridge 在 command 线程上把 stdout/stderr 重定向到缓冲区，调 `cli.main(argv)`，捕获 `SystemExit`，把 `{"code":..,"out":..}` 回给界面。

好处是**零重复**：按钮 = 一条 argv。以后 `dots` 加子命令，界面加个按钮就行，两边都不用改协议。也延续了项目「只有一份命令表」（`cli._parser()`）的既有做法。

`serve` 不走这条路 —— 常驻循环由 bridge 自己接管，`serve --web` / `--web-port` 不出现为按钮。

## 4. 协议

一行一个 JSON，UTF-8，`\n` 分隔。所有行写入都用同一把锁串起来，避免多线程交错出半行。

### Node → Python

| 消息 | 说明 |
|---|---|
| `{"id":1,"cmd":"state"}` | 要一次全量快照 |
| `{"id":2,"cmd":"goal","text":"...","bot":"dot"}` | 下目标 |
| `{"id":3,"cmd":"chat","text":"..."}` | 说一句话 |
| `{"id":4,"cmd":"answer","qid":"q1","value":true}` | 回答聊天里的审批确认 |
| `{"id":5,"cmd":"cli","argv":["doctor"]}` | 跑任意 CLI 子命令 |
| `{"id":6,"cmd":"loop","action":"start"\|"stop"}` | 启停 tick 循环 |
| `{"id":7,"cmd":"quit"}` | 优雅退出 |

### Python → Node

| 消息 | 说明 |
|---|---|
| `{"id":1,"ok":true,"data":{...}}` | 应答 |
| `{"id":1,"ok":false,"error":"..."}` | 失败 |
| `{"event":"ready","version":..,"config":..,"workspace":..,"db":..,"bots":[..],"loop":true,"tick_seconds":60}` | 依赖导入完毕，可以用了。**`loop` / `tick_seconds` 是顶栏那个「● 值守中 60s」和「跑一轮」置灰的唯一来源**，`ready` 不带的话界面没有别的办法查到（发 `loop start` 会顺带启动一次，是有副作用的探测） |
| `{"event":"state","goals":[...],"approvals":[...],"audit":[...],"skills":[...]}` | 全量快照（每轮 tick 完、每次 `goal`/`cli`/`answer` 完各推一次） |
| `{"event":"log","level":"info\|warn\|error","text":"新事件 1 / 跑完 1"}` | 流水 |
| `{"event":"chat_start","text":"..."}` | 开始处理（界面转圈） |
| `{"event":"chat","text":"...","tokens":1234}` | 回答（失败时多一个 `"error":true`） |
| `{"event":"ask","qid":"q1","action":"send_email","args":{...}}` | 聊天里的审批，界面弹确认 |
| `{"event":"output","id":7,"chunk":"...","done":false}` | 命令输出流式追加，最后一条 `done:true` |
| `{"event":"loop_stopped","reason":"quit\|stopped\|crashed"}` | **tick 循环真的停了**。`reason` 只是提示，见约定 6 |
| `{"event":"fatal","where":"tick\|chat\|cmd","error":"..."}` | 某条线程起不来（建图/建库失败、命令线程出错）。见约定 7 |

**`state` 里每一行长什么样**（界面的每一栏都靠它，**字段名不能猜**）：

```python
goals[]     = {id, text, status, origin, parent_id, created_at, updated_at,
               budget, tokens, summary, evidence, bot, handoffs,
               handoff_from, handoff_note}          # 时间戳是 float 秒
approvals[] = {token, goal_id, tool, args, status, created_at, decided_at}
              # 注意是 tool + args,且 args 是 JSON 字符串(不是对象)
audit[]     = {id, ts, goal_id, kind, detail, tokens}
skills[]    = {name, description}
```

三条会让人写错的：

- **审批在两条通道上字段名和类型都不一样。** `state` 里是 `tool` + `args`（**JSON 字符串**，
  要自己 `JSON.parse`）；`ask` 事件里是 `action` + `args`（**已经是对象**）。
  待批横幅（来自 `state`）和确认框（来自 `ask`）是两套解析，别写混。
- **`state` 是截断的**：goals 60 条、audit 60 条，没有 `has_more`。目标多了界面会静默少一行。
- **目标有 7 个状态，§5.4 的颜色表必须覆盖全部**：`pending` / `ready` / `running` /
  `interrupted` / `pending_review` / `done` / `failed`。

**界面必须知道的约定（每一条都是踩出来的）：**

1. **`loop` 的 stop 应答不一定是终态。** 它回的是 `loop_running()` 的真实值：
   - 回 `{"running": false}` = **已经停了**，不要再等事件（循环本来就没跑时不会有 `loop_stopped`）。
   - 回 `{"running": true}` = 线程还在一轮 tick 里，**停下以 `loop_stopped` 事件为准**。

2. **每条命令都有应答，包括 `chat` 和 `answer`。** 七条命令一律按 id 配对。
   - `chat` 回 `{"received": true, "queued": true}` —— 那只是**收下了**，真正的答案走
     `chat_start` / `chat` 事件，这两个事件**带 `id`**，界面按 id 把回答贴回对应的那一问。
   - `answer` 回 `{"received": true|false}`。**`false` 表示这个 qid 已经过期**
     （超过 `ASK_TIMEOUT` 那 600 秒），agent 那边早按拒绝处理了 —— 界面必须如实显示，
     不能因为「请求发出去了」就画成已批准。

3. **行尾是 `\n`**（`bridge.main` 里设了 `newline="\n"`）。如果哪天这条被改回去，
   协议会变成 `\r\n`，Node 侧按行切分必须剥掉尾部 `\r`。

4. **进程退出不发 `exit` 事件。** Node 侧靠子进程的 `exit`/`close` 事件就知道。

5. **界面永远不许暂停或背压子进程的 stdout。** `Out.raw` 是握着写锁做 write+flush 的，
   管道缓冲区（~64KB，`selftest` 的输出远超）一满，写线程会**握着锁阻塞**，
   于是所有线程的应答一起排队 —— 后端看起来还活着（stdin 照样能读、命令照样受理），
   但**任何应答都出不来**，每个按钮一起永久转圈。所以：renderer 不能被节流到不读；
   主进程不许在读流的路径上弹同步框。**「后端还活着吗」只能以子进程 exit 事件为准，
   不能以「命令有应答」为准。**

6. **`reason` 只是提示，`fatal` 才是死因。** 循环崩溃和用户点退出可能挤在同一瞬间，
   此时 `loop_stopped.reason` 未必是 `crashed`。界面要**闩住 `fatal` 事件**
   （收到过就记住），不要靠 `reason` 判断要不要报错。

7. **`fatal` 意味着「进程活着但某条线程死了」，界面必须自己收场**：
   - `where:"tick"` → 顶栏切到「已停止」，别等用户点。
   - `where:"chat"` → **不会再有 `chat` 事件了**，必须把转圈收掉。
   - `where:"cmd"` → 那条命令不会有应答，输出面板要给个失败态。
   §6 的错误表里没有这一行就等于界面永远转圈。

8. **收到 `ready` 之前，一个命令都不许发。** bridge 启动要先导入重依赖（本机 ~12 秒，
   conda 那种装着 torch 的环境更久），这期间它**还没开始读 stdin** —— 发过去的命令会堆在
   管道缓冲区里，等 `ready` 那一刻**一起**被处理。后果是反直觉的先后：实测同时发
   「跑一轮」和「开始值守」，前者被后者抢了锁然后被拒。
   界面在 `ready` 之前必须把所有输入置灰（聊天框、抽屉按钮、批准/拒绝），
   而不是先收下再等 —— 用户点了没反应可以解释，点了然后顺序错乱解释不了。

**状态由 Python 主动推**，界面不轮询：每轮 tick 完、每次 `goal` / `approve` / `cli` 完推一次快照。

这条决定的直接后果：**Node 侧一行 SQL 都不写**。`db.py` 仍然是全项目唯一的 SQL 出口，不破。

## 5. 界面

### 5.1 布局（已选定：上下分区）

```
┌──────────────────────────────────────────────────────┐
│ dots   ● 值守中   60s          [停止]  [体检]  [⚙]   │
├──────────────────────────────────────────────────────┤
│ ⚠ a7f3 想发邮件给 alice ——「周会改到周四」           │
│                              [批准]  [拒绝]           │
├──────────────────────────────────────────────────────┤
│ 目标                    [新建] [刷新] [日志]          │
│  43e86d  ready        把 inbox 整理成报告   29,047    │
│  7f2a1c  done         回 alice 的邮件       12,310    │
│  a91b3e  interrupted  发周报给 alice         8,120    │
│                                                       │
│ 技能                    [学习] [整理]                 │
│  summarize-inbox  Turn a dropped file…   [看] [删]    │
│                                                       │
│ 活动                                                  │
│  15:04  跑完 1 / 新事件 1                             │
│  15:02  gate send_email → ask                         │
├──────────────────────────────────────────────────────┤
│ 对话                                                  │
│  你  工作区里现在有什么?                              │
│  dot inbox/ 里有 3 个文件,最急的是 todo.txt           │
│  ┌────────────────────────────────────────────────┐  │
│  │ 说点什么…                                      │  │
│  └────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────┤
│ ▴ 全部命令   [生成配置][体检][自检][跑一轮][全部目标]… │
└──────────────────────────────────────────────────────┘
```

- 待批横幅**只在有东西等你时出现**，一屏里唯一的强调色就是它
- 目标 / 技能 / 活动从上往下堆，各自带自己的按钮
- 底部聊天常驻；再下面是命令抽屉

### 5.2 命令抽屉

- **默认收起**，只留一条把手 + 最常用的几个按钮
- **手动**：点把手展开 / 收起
- **自动**：窗口高度不够时自动收起（保证聊天区不被挤没）；展开状态记在本地，下次打开还是那样
- **中文标签**，不显示 `init` / `doctor` 这种原始命令名
- **悬浮出 tooltip**：一句中文说明这个按钮干什么，并带出对应的 `dots xxx`

命令映射表（这是按钮的完整清单）：

| 按钮（中文） | 实际命令 | tooltip | 参数 |
|---|---|---|---|
| 生成配置 | `dots init` | 在当前目录生成 dot.toml | — |
| 生成全局配置 | `dots init --global` | 写到 `~/.dots/`，之后在任何目录敲 dots 都能用 | — |
| 体检 | `dots doctor` | 逐项检查配置、密钥、模型、目录、数据库、事件源 | — |
| 自检 | `dots selftest` | 不联网、不调模型，跑一遍内置检查（约 30 秒） | — |
| 跑一轮 | `dots tick` | 手动跑一轮就退（调试用） | — |
| 新建目标 | **原生 `goal` 命令**（不是 `cli goal`） | 交给后台慢慢跑 | 文本输入 |
| 全部目标 | `dots ls` | 列出目标，可按状态筛选 | 可选状态 |
| 活动日志 | `dots log` | 看它最近干了什么 | 可选目标 / 条数 |
| 批准 | `dots approve <编号>` | 批准一个挂起的动作 | 行内，带编号 |
| 拒绝 | `dots reject <编号>` | 拒绝一个挂起的动作 | 行内，带编号 |
| 用量 | `dots cost` | 今日和累计 token 用量 | — |
| 学习 | `dots learn "..."` | 把一段材料总结成技能 | 文本输入 |
| 技能库 | `dots skills` | 列出技能 | — |
| 看技能 | `dots skill show <名>` | 看一份技能的正文 | 行内，带名字 |
| 删技能 | `dots skill rm <名>` | 删掉一份技能 | 行内，带名字 |
| 整理技能 | `dots curate` | 让 agent 合并重复、删掉没价值的 | — |

有参数的按钮点了先出一个小输入框，确认后执行。**危险操作二次确认**：`删技能`、`整理技能`（会真的删改文件）、`生成配置`（会写文件）。

「跑一轮」在 tick 循环运行时置灰，tooltip 说明「后台正在跑，不用手动跑」。

### 5.3 命令输出去哪

点「体检」这类按钮不能没反应。设计：**从底部升起一层输出面板**，等宽、可滚动、可复制、显示退出码，输出流式追加（`selftest` 要跑 30 秒，不能等它跑完才显示）。带「复制」和「关闭」。

### 5.4 视觉方向（已选定：值班台）

| 用途 | 值 |
|---|---|
| 底色 | `#161A20` 深石板蓝（**不是近黑**） |
| 面板 | `#1D222A` |
| 正文 | `#E4E7EB` |
| 次要文字 | `#7C879A` |
| 等你拍板 | `#E0A458` 琥珀（**只给它**） |
| running | `#5FA8D3` |
| ready | `#5FA8D3` |
| done | `#7FA663` |
| interrupted | `#E0A458` |
| failed | `#C4634F` |
| pending | `#8A7BA8` |
| pending_review | `#A08BB8` |

（`ready` 和 `running` 是同一族的两个深浅 —— 它们语义上都是「在动」；
`interrupted` 和「等你拍板」同为琥珀，因为本质上都是「停下来了，等你」。
七个状态一个都不能少，`running` 恰恰是最常看到的那个。）

**五个状态各占一个色相，靠颜色认状态而不是靠亮度。** 一个元素是响的，其它一律安静。

字体：**中文优先**。中文用系统黑体栈（微软雅黑 / 思源黑体），西文和数字配一个打包的等宽/半等宽字体（候选 IBM Plex Mono / Cascadia，随安装包带，约 200KB）。等宽只用在**数字、编号、时间**上 —— 不是整页等宽。

界面文案：中文，动词开头，说清点了会发生什么（「批准」不叫「提交」；按钮叫「整理技能」，出来的结果也叫「整理技能」）。

### 5.5 主题机制（本期只留结构）

所有颜色、字体、间距、圆角**一律走 CSS 自定义属性**，集中在一个 `theme.css` 里：

```css
:root {
  --bg: #161A20;
  --panel: #1D222A;
  --accent-wait: #E0A458;
  --st-ready: #5FA8D3;
  /* ... */
}
```

组件只引用变量，不写死颜色。**本期不做导入 UI**，但因为这个结构，以后「导入主题」= 读一份 CSS 覆盖 `:root` 变量 + 存到本地 + 一个按钮，不用回头改任何组件。

## 6. 错误处理

| 情况 | 界面怎么表现 |
|---|---|
| Python 起不来（没装依赖 / 找不到 python） | 显示 stderr 末尾 + 一句能照做的下一步（`pip install -e .`） |
| 找不到 dot.toml | 复用 `config.load` 的现成报错文案，并高亮「生成配置」按钮 |
| 后端中途崩了 | 显示退出原因 + 一个「重启后端」按钮 |
| 单轮 tick 抛异常 | 照抄 `worker.serve` 的既有策略：吞掉、写 audit、推到流水，**不能干掉整个后端** |
| **某条线程起不来（`fatal`）** | 见下面那条 —— 界面必须自己收场，不能等 |
| 命令超时（比如模型卡住） | 输出面板显示「还在跑…」+ 可中断。（v1 里后端**没有**超时/取消，界面只能靠「后端进程还在不在」判断；这条是已知粗糙处） |
| 关窗口 | 先发 `quit` 优雅退出，**等子进程 exit 事件，最多给 10 秒，超时才 kill** |
| 循环本来没跑时点「停止」 | 应答就是 `{"running": false}`，**没有 `loop_stopped` 事件可等**，别再转圈 |

**为什么是 10 秒而不是 2 秒：** `shutdown()` 的顺序是「先 join 三条线程（每条最多 3 秒），
然后才把 running 的目标复位成 ready」。实测命令线程正在跑 `selftest` 时，`quit` 应答到进程真退出
要 **3.94 秒**。按 2 秒 kill，进程正好死在 join 窗口里 —— 复位被跳过，目标留在 `running`，
而 `recover_stale_runs` 的阈值是 **1800 秒**，于是界面上一条卡片卡在「running」半小时。
最坏情况是 3 条线程各 3 秒，10 秒不虚。

## 7. 测试

沿用项目既有风格：**能单测的纯逻辑单测，端到端用假服务而不是 mock**。

### Python：`tests/test_bridge.py`

- **纯函数级**：`handle()` 处理 `goal` / `approve` / `state` / 未知命令 / 格式错误的 JSON 行
- **协议级**：用 `StringIO` 替掉 `sys.stdin` / `stdout`，喂一串行给 `main()`，断言吐出来的 JSON 行
- **线程级**：tick 循环能 start / stop；聊天里 `ask` 的往返（用假 agent）
- **退出**：`quit` 之后 running 的目标真的被复位成 ready

现有 24 个测试必须继续全绿。

### Node：`desktop/electron/protocol.test.js`

用 Node 内置的 `node --test`，只测纯逻辑，不引测试框架：

- 行缓冲解析器（半行、粘包、一行多个 JSON、非 UTF-8 边界、超长行）
- `resolveBackend()` 的查找顺序

### 手工验收清单

1. 起界面 → 后端状态从「启动中」变「值守中」
2. 往 `workspace/inbox/` 扔文件 → 流水里出现新事件 → 目标列表自动多一行
3. 点「批准」→ 目标续跑
4. 聊天里问一句 → 有回答；聊天里触发发邮件 → 界面弹确认框
5. 点「体检」→ 输出面板有内容
6. 关窗口 → 进程树里没有残留 python

## 8. 打包与目录

```
dots-agent/
├── dots/
│   ├── bridge.py                ★ 新增
│   ├── graph.py                 ← 改：CURRENT 线程局部
│   └── cli.py                   ← 改：加 bridge 子命令、惰性导入
├── desktop/                     ★ 新增：Electron 应用
│   ├── package.json
│   ├── electron/
│   │   ├── main.js              窗口 + 生命周期 + IPC
│   │   ├── backend.js           子进程管理 + 协议
│   │   ├── protocol.js          纯函数：编解码 / 行缓冲
│   │   └── preload.js           contextBridge 白名单
│   ├── renderer/
│   │   ├── index.html
│   │   ├── theme.css            设计变量（主题的落点）
│   │   ├── style.css
│   │   └── app.js               store + 渲染，无框架
│   └── assets/icon.ico          图标（缺失则用 Electron 默认）
├── release/                     ★ 安装包输出（gitignore）
├── docs/superpowers/specs/      本文档
└── tests/test_bridge.py         ★ 新增
```

- 打包：**electron-builder**，NSIS 安装包，Windows x64，输出到仓库根的 `release/`
  （`directories.buildResources` 指到 `assets`，不用默认的 `build/` —— 那个名字和 .gitignore 里 Python 段的 `build/` 撞车，图标会被静默忽略掉）
- 开发模式：`npm start`，用系统 Python（`DOTS_PYTHON` 环境变量可覆盖）
- `.gitignore` 增加：`desktop/node_modules/`、`desktop/dist/`、`release/`、`.superpowers/`

### 「怎么找到 Python」只在一个地方

`protocol.js` 之外的 `backend.js` 里有个 `resolveBackend()`，是**唯一**决定用什么命令起后端的地方：

1. `DOTS_PYTHON` 环境变量（开发/调试用）
2. 项目 venv（`.venv/Scripts/python.exe`）
3. PATH 上的 `python`

以后做「自带运行时发给别人」，**只改这一个函数**：指向安装包里的 `resources/python/python.exe`。这也是为什么本期先做开发版但不会把路走死。

## 9. 安全

- `contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`
- `preload.js` 只暴露白名单方法，不暴露原始 `ipcRenderer`
- `index.html` 加 CSP，不加载任何远程内容（字体随包带，不走 CDN）
- 不开生产环境的 devtools
- 工作区仍是 `FilesystemBackend(root_dir=..., virtual_mode=True)`，界面不绕过它

## 10. 分期

**v1（本期）**：上面全部。开发模式用系统 Python 跑起来，功能完整，能出 NSIS 安装包。

**以后**：
- 主题导入 UI（结构已留好）
- 自带 Python 运行时，安装包发给别人
- 聊天流式输出（v1 只有「正在想…」）
- 聊天里显示 agent 正在调哪个工具

### 已知的粗糙处（v1 接受）

- 后端启动要等 2.2–8.7 秒（取决于是不是 conda base 那种装着 torch 的环境），期间界面只能显示「正在启动后端…」
- 聊天不带流式，慢模型下会等十几秒，只有转圈
- 中文界面 + 桌面打包不能走 CDN 字体，中文只能用系统黑体栈（西文数字用随包的字体补个性）
- **命令没有超时、没有取消。** 界面只能显示「还在跑…」，要中断只能重启后端。
  （一条卡住的命令在当前实现里不会堵死别的命令 —— 命令线程是独立的 —— 但它自己永远不返回。）
- **批准 / 拒绝要等下一轮 tick 才真的续跑**（`worker.tick` 第 3 步统一处理）。
  循环没在跑时批准一个挂起动作**什么都不会发生**，而快照里那行审批已经消失，看起来像成功了。
  界面最好在循环停着时给一句提示 —— 这也是 `ready` 必须带 `loop` 的原因之一。
- **「两个真 provider 长调用并发」这条没验过。** 框架层验过了：两条线程各自建真的
  deepagents 图（假 BaseChatModel）、各自 invoke，1.56s / 1.67s 建完、都成功、无异常。
  但「一个目标正在跑真模型的同时另一条线程也在跑真模型」还没实测 —— 写界面之前补一次。
