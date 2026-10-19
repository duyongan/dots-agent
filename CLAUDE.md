# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

常驻主动式 agent:进程在后台反复 tick,自己从事件源发现问题、建目标、跑目标,产出落在工作区。
包名 `dots-agent`,命令 `dots`。Python ≥3.11,依赖 deepagents / langgraph(SQLite checkpointer)/ langchain。

代码注释、docstring、面向用户的输出一律中文;给模型看的提示词(`SYSTEM_PROMPT` / bot prompt)是英文,
`CHAT_PROMPT`、`LEARN_PROMPT`、`CURATE_PROMPT` 是中文。

## 常用命令

```bash
pip install -e ".[test]"        # 开发装法;test extra = pytest + cryptography(假 IMAP 用自签证书)

python -m pytest tests/ -q                 # 全套(约 30s)
python -m pytest tests/test_loop.py -q     # 只跑一个文件
python -m pytest tests/test_sources.py::test_imap_poll_over_tls -q   # 只跑一个用例
python -m tests.test_loop                   # 直接跑,打印每个用例的一句话结论
python -m dots selftest                     # 不联网不调模型:跑四个 demo() + 全套 tests(5 节)

python -m dots doctor                       # 体检(配置/密钥/模型/目录/DB/事件源)
python -m dots tick                         # 手动跑一轮就退
python -m dots serve --web                  # 常驻 + 活动视图 http://127.0.0.1:8765/
```

pytest 会报一批 `PytestReturnNotNoneWarning`,这是**预期的**:每个用例 return 一句中文结论,
给 `dots selftest` 用。不要为了消警告把它们改成断言。

没有配置 lint/format 工具,手工守 PEP8。版本号在 `dots/__init__.py:__version__`,
pyproject 动态读它;提交信息格式是 `<版本>:<一句话改动>`。

## 架构

### 一轮 tick(worker.py 是唯一编排者)

`worker.tick()` 四步,顺序不能换:

1. `sources.poll_all()` 收事件,`db.add_event` 落库(`UNIQUE(source, key)` 就是幂等)
2. 路由新事件:审批回信 → 写决定;其它 → 建 ready 目标
3. 已决定的审批 → 续跑对应目标(先让超时的审批按拒绝过期)
4. 挑一个 ready 目标跑一轮

CLI 和 sources **都不许直接调 graph**,一律经 worker。`worker.serve()` 是外层循环,
每轮 `recover_stale_runs()` 把被 kill 后卡在 running 的目标放回 ready(checkpoint 还在,接着跑不是重跑)。

### 没有 IPC

`dots goal` 往 goals 表插一行,`dots approve` 改 approvals 表一行,常驻进程下一轮自己看见。
邮件回复和 CLI 批准**共用同一条路径**:都只是往 approvals 表写决定,由第 3 步统一续跑。

### 两道闸门,别搞混(graph.py)

- `rules.decide(action, rules)` — 纯函数,`allow/ask/deny`,精确名 > 通配 > 默认 ask
- `interrupt()` — langgraph 原语,第一次调用中断整张图交出控制权,续跑时「返回」resume 值

`_gate()` 里 interrupt 之后的代码只在人批了之后才执行,这是整套审批的地基。

### 全局状态与图

- `graph.CURRENT` 是**线程局部**的,访问器是 `current()` / `set_current()` / `use_or_create()`。
  一个 `RunCtx` 是 `(cfg, conn, goal_id, bot, chat)`,工具都是闭包拿它。
  之所以不是模块全局:桌面界面的 bridge 里 tick 和聊天跑在两条线程上,
  两边的 `goal_id` / `bot` / `chat` 都不同,模块全局会被互相踩
- `chat=True` 时生命周期工具(`complete_goal` / `handoff_to`)改成说实话而不是空转,
  `chat_send` 用完必须在 `finally` 里复位,否则后台目标会以为自己在聊天
- 每个 bot 一张编译好的图,**共用同一个 checkpointer**。thread_id = goal id,
  所以交接是同一条对话线上换个人接手,新 bot 看得到前任的全部消息
- `_pick()` 同时接受 `{bot: 图}` 和单个图(测试用桩只给一个)

### 数据库(db.py 是唯一写 SQL 的地方)

四张表 + meta:`goals` / `events` / `approvals` / `audit`。`audit` 既是活动视图数据源也是成本统计源。

- 目标状态机:`pending`(agent 派生,要人工批)→ `ready` → `running` → `interrupted` / `pending_review` / `done` / `failed`
- 抢目标靠 `claim_goal()` 的 `WHERE status = ?`,rowcount=0 就是没抢到,不需要额外的锁
- checkpointer、web、每请求各开**自己的一条 sqlite 连接**。跟应用库共用会互相 commit 掉对方的事务
  (`cannot commit - no transaction is active`)。Windows 上连接不关就删不掉 db 文件
- 加字段:DDL 是 `CREATE TABLE IF NOT EXISTS`,对老库无效 —— 必须往 `MIGRATIONS` 里加对应的 ALTER 并抬 `SCHEMA_VERSION`

### 自激循环(最容易踩的坑)

agent 往被监听的目录写的东西会被自己的 fs 源当成下一轮新事件。`worker._suppress_self_writes()`
把本轮新增的文件 key 以 `status='self'` 预插进 events 表挡掉。
它必须在 `finally` 里 —— 失败的 run 也会留下半成品文件。

### 事件源(sources.py)

只读外部世界,**绝不写数据库**,落库是 worker 的事。每个源独立 try,一个挂了不能拖垮整轮。
加第四个源 = 写一个 `poll(cfg, conn, cursor) -> (events, new_cursor)`,别的文件一行不改。
`fs_scan()` 被 worker 共用(查 agent 自己写了什么),别在两处各写一遍 key 的拼法。

### 审批(approvals.py)

邮箱同时是事件源和审批通道,靠主题前缀 `[dot#` 区分。
**dot 自己发的审批邮件会被 imap_poll 再收一次**,必须靠前缀拦下,否则它给自己回信回一晚上。

### 技能(skills.py + prompts.py)

`<workspace>/<dir>/<名>/SKILL.md`,deepagents 原生格式(YAML frontmatter)。
description 每轮都进系统提示并**被截断在 60 字符**,所以校验卡得很死;
提示词里写一遍是提醒,真正拦人的是 `skills.validate()`。

### 配置(config.py 是唯一读 dot.toml 的地方)

查找顺序:`-c` 指定 → `./dot.toml` → `~/.dots/dot.toml`。**`-c` 指了就不兜底**,打错字直接报错。
所有相对路径相对配置文件所在目录 —— 所以 `init --global` 的数据落在 `~/.dots/`。
密钥只写环境变量名,不写进文件。`template.toml` 是 `init` 生成的母本,改了模板要记得它在 package-data 里。

## 硬约束(改动时别破坏)

- `FilesystemBackend(..., virtual_mode=True)` 是必须的。默认 False 时绝对路径和 `..` 能绕出 root_dir,
  这个「沙箱」就只是句好话
- `web.py` 只听 127.0.0.1、没有鉴权。任何情况下别改成对外 host
- `gui.py`(网页界面,React 构建产物在 `dots/webui/`)也只听 127.0.0.1;
  写接口校验 `X-Dots-Token` 和 Host/Origin。同样别改成对外 host。
  它和桌面 bridge 是两套入口,后端逻辑都复用 `db` / `worker` / `skills`
- `run_shell` 是 `shell=True` + 只靠 cwd 兜着,**不是沙箱**;默认规则是 deny
- 活动视图里所有来自模型/邮件/工具调用的文本必须 `_esc()` 转义(有 XSS 测试盯着)
- 密钥、`dot.toml`、`dot.db`、`workspace/` 都在 .gitignore 里,不要提交

## 代码里的约定

- 注释里出现 `ponytail:` 是**作者标记的有意为之的将就**(已知限制 + 何时该替换)。
  看到它不要当 bug 修,除非确实到了它写明的替换条件
- `rules.py` / `sources.py` / `approvals.py` / `skills.py` 各有一个 `demo()`,
  纯逻辑零依赖,`dots selftest` 会调。改这几个模块的纯逻辑时顺手更新 demo 里的断言
- CLI 只有一份命令表(`cli._parser()`),交互终端把 `/xxx` 原样再喂给 `main()`,
  所以交互里能用的和命令行完全一样。加命令只改 `_parser()` 和 `_dispatch()`
- `cli._table()` 用 `_w()` 算东亚字符宽度(中文占两格),表格对齐靠它,别用 `len()`
